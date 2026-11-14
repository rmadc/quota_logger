"""Copy existing Codex and Claude SQLite histories into the shared schema."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sqlite3
import sys
from dataclasses import dataclass

from .database import open_database


def local_data_dir() -> pathlib.Path:
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        root = pathlib.Path.home() / "AppData" / "Local"
    return pathlib.Path(root)


DEFAULT_SOURCES = {
    "codex": local_data_dir() / "Codex Quota Logger" / "quota-history.sqlite3",
    "claude": local_data_dir() / "Claude Quota Logger" / "quota-history.sqlite3",
}

WINDOWS = (
    ("five_hour", "5h", "five_pct", "five_reset", 5 * 60 * 60),
    ("weekly", "7d", "week_pct", "week_reset", 7 * 24 * 60 * 60),
)


@dataclass(frozen=True)
class ImportReport:
    provider: str
    source_rows: int
    imported_rows: int
    already_imported_rows: int
    imported_windows: int
    first_sample_utc: str | None
    last_sample_utc: str | None


def open_source(path: pathlib.Path | str) -> sqlite3.Connection:
    """Open a legacy DB read-only and pin a consistent WAL-aware snapshot."""
    source = pathlib.Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Source database not found: {source}")
    uri = source.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("BEGIN")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(quota_samples)")}
    required = {"id", "timestamp_utc", "five_pct", "week_pct", "five_reset",
                "week_reset", "ok", "error", "raw_json"}
    if not required.issubset(columns):
        conn.close()
        missing = ", ".join(sorted(required - columns))
        raise RuntimeError(f"Unsupported source schema in {source}; missing: {missing}")
    return conn


def normalize_timestamp(value: str) -> str:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # The legacy column is explicitly named timestamp_utc; treat any
        # naive historical value as UTC rather than the machine's local time.
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _source_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM quota_samples").fetchone()[0]


def _import_one(target: sqlite3.Connection, provider: str,
                source: sqlite3.Connection) -> ImportReport:
    if provider not in ("codex", "claude"):
        raise ValueError(f"Unsupported provider key: {provider}")
    source_rows = _source_count(source)
    origin_key = f"legacy:{provider}:quota_samples"
    imported = 0
    skipped = 0
    window_count = 0
    first_dt = None
    last_dt = None
    first = None
    last = None

    source_cursor = source.execute("""
        SELECT id, timestamp_utc, five_pct, week_pct, five_reset, week_reset,
               ok, error, raw_json
        FROM quota_samples ORDER BY id
    """)
    provider_id = target.execute(
        "SELECT provider_id FROM providers WHERE provider_key = ?", (provider,)
    ).fetchone()[0]

    while True:
        batch = source_cursor.fetchmany(1000)
        if not batch:
            break
        for row in batch:
            source_id, stamp, five, week, five_reset, week_reset, ok, error, raw_json = row
            normalized_stamp = normalize_timestamp(stamp)
            parsed_stamp = dt.datetime.fromisoformat(normalized_stamp.replace("Z", "+00:00"))
            if first_dt is None or parsed_stamp < first_dt:
                first_dt, first = parsed_stamp, normalized_stamp
            if last_dt is None or parsed_stamp > last_dt:
                last_dt, last = parsed_stamp, normalized_stamp
            result = target.execute("""
                INSERT INTO samples
                    (provider_id, observed_at_utc, ok, error, raw_json,
                     origin_key, origin_sample_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(origin_key, origin_sample_id) DO NOTHING
            """, (provider_id, normalized_stamp, int(ok), error, raw_json,
                  origin_key, source_id))
            if result.rowcount == 0:
                skipped += 1
                continue
            imported += 1
            if not ok:
                continue
            sample_id = result.lastrowid
            for key, label, value_column, reset_column, duration in WINDOWS:
                value = five if value_column == "five_pct" else week
                reset = five_reset if reset_column == "five_reset" else week_reset
                if value is None and reset is None:
                    continue
                target.execute("""
                    INSERT INTO quota_windows
                        (sample_id, window_key, window_label, used_pct,
                         reset_at_epoch, window_seconds)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (sample_id, key, label, value, reset, duration))
                window_count += 1

    migrated, target_first, target_last = target.execute("""
        SELECT COUNT(*), MIN(observed_at_utc), MAX(observed_at_utc) FROM samples
        WHERE origin_key = ?
    """, (origin_key,)).fetchone()
    if migrated != source_rows:
        raise RuntimeError(
            f"{provider}: source has {source_rows} rows but destination has {migrated} imported rows."
        )
    if (target_first, target_last) != (first, last):
        raise RuntimeError(
            f"{provider}: source and destination timestamp ranges differ: "
            f"source {first} .. {last}; destination {target_first} .. {target_last}."
        )
    return ImportReport(provider, source_rows, imported, skipped, window_count, first, last)


def import_databases(output: pathlib.Path | str,
                     sources: dict[str, pathlib.Path | str]) -> list[ImportReport]:
    """Idempotently import each provider in one target transaction."""
    opened_sources: list[sqlite3.Connection] = []
    target = open_database(output)
    try:
        source_pairs = []
        for provider, path in sources.items():
            source = open_source(path)
            opened_sources.append(source)
            source_pairs.append((provider, source))
        with target:
            return [_import_one(target, provider, source)
                    for provider, source in source_pairs]
    finally:
        for source in opened_sources:
            source.close()
        target.close()


def format_report(report: ImportReport) -> str:
    first = report.first_sample_utc or "none"
    last = report.last_sample_utc or "none"
    return (f"{report.provider}: {report.source_rows:,} samples verified; "
            f"{report.imported_rows:,} imported, "
            f"{report.already_imported_rows:,} already present, "
            f"{report.imported_windows:,} quota windows imported; "
            f"range {first} .. {last}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-db", type=pathlib.Path, default=DEFAULT_SOURCES["codex"])
    parser.add_argument("--claude-db", type=pathlib.Path, default=DEFAULT_SOURCES["claude"])
    parser.add_argument("--output", type=pathlib.Path, required=True,
                        help="Destination SQLite database (must not be either source DB)")
    args = parser.parse_args(argv)

    output = args.output.expanduser().resolve()
    sources = {"codex": args.codex_db.expanduser().resolve(),
               "claude": args.claude_db.expanduser().resolve()}
    if output in sources.values():
        parser.error("--output must differ from both source databases")
    try:
        reports = import_databases(output, sources)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    for report in reports:
        print(format_report(report))
    print(f"Unified database: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
