"""Poll all configured providers independently into the shared SQLite DB."""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sqlite3
import sys
import time
from collections.abc import Iterable

from .database import open_database, record_sample
from .providers import DEFAULT_ADAPTERS
from .providers.base import ProviderAdapter
from .providers.claude import ClaudeAuthenticationError, ClaudeRateLimitError


def local_data_dir() -> pathlib.Path:
    import os

    root = os.environ.get("LOCALAPPDATA")
    if not root:
        root = pathlib.Path.home() / "AppData" / "Local"
    return pathlib.Path(root)


DEFAULT_DATABASE = local_data_dir() / "Quota Logger" / "quota-history.sqlite3"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _display_value(value: float | None) -> str:
    return "-" if value is None else f"{value:g}%"


def record_refresh_event(conn: sqlite3.Connection, adapter: ProviderAdapter) -> None:
    """Append token-change metadata beside the DB, without recording credentials."""
    event = getattr(adapter, "refresh_event", None)
    if not event:
        return
    try:
        database_file = conn.execute("PRAGMA database_list").fetchone()[2]
        if database_file:
            path = pathlib.Path(database_file).parent / "collector-events.log"
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"{utc_now()}  {event}\n")
    except (OSError, sqlite3.Error) as exc:
        print(f"Could not write collector event: {exc}", file=sys.stderr, flush=True)
    finally:
        adapter.refresh_event = None


def collect_provider(conn: sqlite3.Connection, adapter: ProviderAdapter,
                     quiet: bool = False, observed_at_utc: str | None = None,
                     retry_deadlines: dict[str, float] | None = None,
                     rejected_tokens: dict[str, str] | None = None) -> bool:
    """Poll one provider; persist its failure without blocking other providers."""
    stamp = observed_at_utc or utc_now()
    try:
        payload = adapter.fetch_usage()
        windows = adapter.parse_response(payload)
    except Exception as exc:
        if isinstance(exc, ClaudeRateLimitError) and retry_deadlines is not None:
            retry_deadlines[adapter.provider_key] = exc.retry_at
        if isinstance(exc, ClaudeAuthenticationError) and rejected_tokens is not None:
            rejected_tokens[adapter.provider_key] = exc.token_fingerprint
        error = f"{type(exc).__name__}: {exc}"[:500]
        record_sample(conn, adapter.provider_key, adapter.display_name, stamp, error=error)
        if not quiet:
            print(f"{stamp}  {adapter.display_name}: ERROR: {error}",
                  file=sys.stderr, flush=True)
        return False
    finally:
        record_refresh_event(conn, adapter)

    record_sample(conn, adapter.provider_key, adapter.display_name, stamp,
                  windows=windows, raw_payload=payload)
    if not quiet:
        values = "  ".join(f"{window.label}: {_display_value(window.used_pct)}"
                           for window in windows)
        print(f"{stamp}  {adapter.display_name}: {values}", flush=True)
    return True


def collect_once(conn: sqlite3.Connection, adapters: Iterable[ProviderAdapter],
                 quiet: bool = False,
                 retry_deadlines: dict[str, float] | None = None,
                 rejected_tokens: dict[str, str] | None = None) -> dict[str, bool]:
    """Collect providers sequentially, isolating fetch/parse failures per provider."""
    results = {}
    for adapter in adapters:
        if retry_deadlines is not None and time.time() < retry_deadlines.get(adapter.provider_key, 0):
            continue
        if rejected_tokens is not None and adapter.provider_key in rejected_tokens:
            fingerprint = adapter.current_token_fingerprint()
            if fingerprint is None or fingerprint == rejected_tokens[adapter.provider_key]:
                continue
            del rejected_tokens[adapter.provider_key]
        results[adapter.provider_key] = collect_provider(conn, adapter, quiet=quiet,
                                                         retry_deadlines=retry_deadlines,
                                                         rejected_tokens=rejected_tokens)
    return results


def pending_retry_deadlines(conn: sqlite3.Connection) -> dict[str, float]:
    """Restore an unresolved rate limit after the collector restarts."""
    rows = conn.execute("""
        SELECT p.provider_key, s.ok, s.error FROM providers p JOIN samples s
          ON s.sample_id = (SELECT MAX(sample_id) FROM samples WHERE provider_id = p.provider_id)
        WHERE p.provider_key = 'claude'
    """).fetchall()
    deadlines = {}
    for key, ok, error in rows:
        match = re.search(r"HTTP 429\); retry_at=(\d+(?:\.\d+)?)", error or "")
        if not ok and match and float(match.group(1)) > time.time():
            deadlines[key] = float(match.group(1))
    return deadlines


def pending_rejected_tokens(conn: sqlite3.Connection) -> dict[str, str]:
    """Keep a rejected token paused if the collector restarts."""
    row = conn.execute("""
        SELECT s.ok, s.error FROM samples s JOIN providers p USING(provider_id)
        WHERE p.provider_key = 'claude' ORDER BY s.sample_id DESC LIMIT 1
    """).fetchone()
    if row and not row[0]:
        match = re.search(r"HTTP 401\).*token_fingerprint=([0-9a-f]{64})", row[1] or "")
        if match and "refresh_attempted=1" in (row[1] or ""):
            return {"claude": match.group(1)}
    return {}


def run(database: pathlib.Path, interval_seconds: int = 60, once: bool = False,
        quiet: bool = False, adapters: Iterable[ProviderAdapter] = DEFAULT_ADAPTERS) -> int:
    if interval_seconds < 1:
        raise ValueError("Polling interval must be at least one second")
    adapters = tuple(adapters)
    conn = open_database(database)
    retry_deadlines = pending_retry_deadlines(conn)
    rejected_tokens = pending_rejected_tokens(conn)
    if not quiet:
        print(f"Database: {database.expanduser().resolve()}")
        print(f"Providers: {', '.join(adapter.display_name for adapter in adapters)}")
        print(f"Polling: every {interval_seconds} seconds; Ctrl+C to stop.", flush=True)
    try:
        while True:
            started = time.monotonic()
            collect_once(conn, adapters, quiet=quiet, retry_deadlines=retry_deadlines,
                         rejected_tokens=rejected_tokens)
            if once:
                break
            time.sleep(max(0, interval_seconds - (time.monotonic() - started)))
    except KeyboardInterrupt:
        if not quiet:
            print("\nStopped.")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DATABASE)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true",
                        help="Poll each provider once and exit (makes live API requests)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress console output for background operation")
    args = parser.parse_args(argv)
    try:
        return run(args.db, args.interval_seconds, args.once, args.quiet)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"Collector could not start: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
