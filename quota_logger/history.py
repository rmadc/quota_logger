"""Compact, provider-neutral history viewer for the shared quota database."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field

from .collector import DEFAULT_DATABASE
from .display import (ansi_color, date_separator, local_time,
                      quota_pressure, reset_countdown, weekly_pressure)


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class WindowDefinition:
    key: str
    label: str
    duration: int | None


@dataclass
class HistorySample:
    sample_id: int
    provider_id: int
    provider_key: str
    provider_name: str
    observed_at_utc: str
    ok: bool
    error: str | None
    windows: dict[str, tuple[str, float | None, int | None, int | None]] = field(default_factory=dict)


def connect_readonly(path: pathlib.Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"History database not found: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def window_definitions(conn: sqlite3.Connection) -> list[WindowDefinition]:
    rows = conn.execute("""
        SELECT window_key, window_label, MAX(window_seconds)
        FROM quota_windows
        GROUP BY window_key, window_label
    """).fetchall()
    preferred = {"five_hour": 0, "weekly": 1}
    rows.sort(key=lambda row: (preferred.get(row[0], 2), row[0], row[1]))
    result = []
    seen = set()
    for key, label, duration in rows:
        if key in seen:
            continue
        seen.add(key)
        result.append(WindowDefinition(key, label, duration))
    return result


def fetch_samples(conn: sqlite3.Connection, limit: int | None = None,
                  after_id: int | None = None, include_errors: bool = False) -> list[HistorySample]:
    where = []
    params: list[object] = []
    if after_id is not None:
        where.append("s.sample_id > ?")
        params.append(after_id)
    if not include_errors:
        where.append("s.ok = 1")
    if limit is not None and after_id is None:
        selected = "s.sample_id IN (SELECT sample_id FROM samples"
        if not include_errors:
            selected += " WHERE ok = 1"
        selected += " ORDER BY observed_at_utc DESC, sample_id DESC LIMIT ?)"
        where.append(selected)
        params.append(limit)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(f"""
        SELECT s.sample_id, s.provider_id, p.provider_key, p.display_name,
               s.observed_at_utc, s.ok, s.error, w.window_key, w.window_label,
               w.used_pct, w.reset_at_epoch, w.window_seconds
        FROM samples AS s
        JOIN providers AS p USING(provider_id)
        LEFT JOIN quota_windows AS w USING(sample_id)
        {clause}
        ORDER BY s.observed_at_utc, s.sample_id, w.window_key
    """, params).fetchall()
    grouped: dict[int, HistorySample] = {}
    for row in rows:
        (sample_id, provider_id, provider_key, provider_name, stamp, ok, error,
         window_key, label, used, reset, duration) = row
        sample = grouped.setdefault(sample_id, HistorySample(
            sample_id, provider_id, provider_key, provider_name, stamp, bool(ok), error
        ))
        if window_key is not None:
            sample.windows[window_key] = (label, used, reset, duration)
    return list(grouped.values())


def latest_sample_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(sample_id), 0) FROM samples").fetchone()[0]


def provider_window_history(conn: sqlite3.Connection, sample: HistorySample,
                            window_key: str, reset: int, sample_epoch: float):
    start = dt.datetime.fromtimestamp(sample_epoch - 3 * 3600, dt.timezone.utc).isoformat()
    rows = conn.execute("""
        SELECT s.sample_id, s.observed_at_utc, w.used_pct, w.reset_at_epoch
        FROM samples AS s
        JOIN quota_windows AS w USING(sample_id)
        WHERE s.provider_id = ? AND s.ok = 1 AND w.window_key = ?
          AND w.reset_at_epoch = ? AND s.sample_id <= ?
          AND julianday(s.observed_at_utc) >= julianday(?)
        ORDER BY s.sample_id
    """, (sample.provider_id, window_key, reset, sample.sample_id, start)).fetchall()
    history = []
    for sample_id, stamp, used, reset_epoch in rows:
        parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        history.append((sample_id, parsed.timestamp(), used, reset_epoch))
    return history


def _visible_width(value: str) -> int:
    return len(ANSI_RE.sub("", value))


def _format_cell(value: str, width: int, right: bool = False) -> str:
    visible = _visible_width(value)
    spaces = " " * max(0, width - visible)
    return spaces + value if right else value + spaces


def build_layout(samples: list[HistorySample], windows: list[WindowDefinition]):
    provider_width = max([8] + [len(sample.provider_name) for sample in samples])
    columns: list[tuple[str, int, bool]] = [
        ("Time", 5, False), ("Provider", provider_width, False), ("State", 5, False),
    ]
    for window in windows:
        value_width = max(5, len(window.label))
        eta_label = f"{window.label} ETA"
        eta_width = max(7 if (window.duration or 0) >= 86400 else 8, len(eta_label))
        columns.extend(((window.label, value_width, True), (eta_label, eta_width, True)))
    header = " | ".join(_format_cell(label, width, right)
                         for label, width, right in columns)
    width = max(40, len(header))
    return columns, header.ljust(width), width


def _sample_day(sample: HistorySample) -> str:
    value = dt.datetime.fromisoformat(sample.observed_at_utc.replace("Z", "+00:00"))
    return value.astimezone().date().isoformat()


def _window_pressure(conn, sample, window_key, value, reset, duration):
    if window_key == "weekly":
        history = ()
        sample_epoch = None
        if reset is not None:
            parsed = dt.datetime.fromisoformat(sample.observed_at_utc.replace("Z", "+00:00"))
            sample_epoch = parsed.timestamp()
            history = provider_window_history(conn, sample, window_key, reset, sample_epoch)
        return weekly_pressure(value, reset, sample.observed_at_utc, duration,
                               history=history, sample_id=sample.sample_id)
    return quota_pressure(value, reset, sample.observed_at_utc, duration)


def format_sample(conn: sqlite3.Connection, sample: HistorySample,
                  windows: list[WindowDefinition], columns, color: bool) -> str:
    by_key = sample.windows
    time_text = local_time(sample.observed_at_utc)
    state_text = "OK" if sample.ok else "ERROR"
    cells = [time_text, sample.provider_name, state_text]
    for definition in windows:
        current = by_key.get(definition.key)
        if current is None:
            cells.extend(("-", "-"))
            continue
        _label, used, reset, duration = current
        pct = "-" if used is None else f"{used:g}%"
        pressure = _window_pressure(conn, sample, definition.key, used, reset, duration)
        cells.append(ansi_color(pct, pressure, color))
        eta = reset_countdown(reset, sample.observed_at_utc, duration)
        if reset is None and definition.key == "five_hour" and used == 0:
            eta = "05h:00m"
        cells.append(eta)
    line = " | ".join(_format_cell(value, width, right)
                       for value, (_, width, right) in zip(cells, columns))
    table_width = max(40, sum(column[1] for column in columns) + 3 * (len(columns) - 1))
    return line + " " * max(0, table_width - _visible_width(line))


def print_header(header: str, width: int):
    print(header)
    print("-" * width)


def print_samples(conn: sqlite3.Connection, samples: list[HistorySample],
                  windows: list[WindowDefinition], color: bool,
                  previous_day: str | None = None) -> str | None:
    columns, header, width = build_layout(samples, windows)
    last_day = previous_day
    for sample in samples:
        day = _sample_day(sample)
        if day != last_day:
            if last_day is not None:
                print()
            print(date_separator(day, width, color))
            last_day = day
        print(format_sample(conn, sample, windows, columns, color), flush=True)
    return last_day


def display_database_path(path: pathlib.Path) -> str:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        try:
            relative = path.resolve().relative_to(pathlib.Path(local_app_data).resolve())
            return "%LOCALAPPDATA%\\" + str(relative)
        except ValueError:
            pass
    return str(path.resolve())


def ensure_legend_encoding() -> None:
    """Use UTF-8 when the active output encoding cannot print the legend glyph."""
    if not hasattr(sys.stdout, "reconfigure"):
        return
    encoding = sys.stdout.encoding or "utf-8"
    try:
        "◆".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        sys.stdout.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample_count", type=int, nargs="?",
                        help="Number of samples to show initially (default: 100)")
    parser.add_argument("-N", "--count", dest="count", type=int,
                        help="Number of samples to show initially (default: 100)")
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DATABASE)
    parser.add_argument("--follow", action="store_true", help="Append new samples as they arrive")
    parser.add_argument("--errors", action="store_true", help="Include failed polls")
    parser.add_argument("--no-color", action="store_true", help="Disable quota colors")
    args = parser.parse_args(argv)
    limit = args.count if args.count is not None else (
        args.sample_count if args.sample_count is not None else 100
    )
    if limit < 1:
        parser.error("N must be positive")
    ensure_legend_encoding()
    try:
        conn = connect_readonly(args.db)
    except (OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    color = not args.no_color and sys.stdout.isatty()
    try:
        conn.execute("BEGIN")
        last_id = latest_sample_id(conn)
        initial = fetch_samples(conn, limit=limit, include_errors=args.errors)
        windows = window_definitions(conn)
        _columns, header, width = build_layout(initial, windows)
        conn.commit()
        print(f"Database: {display_database_path(args.db)}")
        print_header(header, width)
        previous_day = print_samples(conn, initial, windows, color)
        if not initial:
            print("No quota history yet.")
        if not args.follow:
            return 0

        while True:
            time.sleep(2)
            conn.execute("BEGIN")
            try:
                new_samples = fetch_samples(conn, after_id=last_id, include_errors=args.errors)
                new_last_id = latest_sample_id(conn)
                current_windows = window_definitions(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            if [(w.key, w.label) for w in current_windows] != [(w.key, w.label) for w in windows]:
                windows = current_windows
                _columns, header, width = build_layout(new_samples, windows)
                print("\nQuota windows changed; updated columns:")
                print_header(header, width)
                previous_day = None
            previous_day = print_samples(conn, new_samples, windows, color, previous_day)
            last_id = new_last_id
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
