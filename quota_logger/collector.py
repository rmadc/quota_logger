"""Poll all configured providers independently into the shared SQLite DB."""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys
import time
from collections.abc import Iterable

from .database import open_database, record_sample
from .providers import DEFAULT_ADAPTERS
from .providers.base import ProviderAdapter


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


def collect_provider(conn: sqlite3.Connection, adapter: ProviderAdapter,
                     quiet: bool = False, observed_at_utc: str | None = None) -> bool:
    """Poll one provider; persist its failure without blocking other providers."""
    stamp = observed_at_utc or utc_now()
    try:
        payload = adapter.fetch_usage()
        windows = adapter.parse_response(payload)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        record_sample(conn, adapter.provider_key, adapter.display_name, stamp, error=error)
        if not quiet:
            print(f"{stamp}  {adapter.display_name}: ERROR: {error}",
                  file=sys.stderr, flush=True)
        return False

    record_sample(conn, adapter.provider_key, adapter.display_name, stamp,
                  windows=windows, raw_payload=payload)
    if not quiet:
        values = "  ".join(f"{window.label}: {_display_value(window.used_pct)}"
                           for window in windows)
        print(f"{stamp}  {adapter.display_name}: {values}", flush=True)
    return True


def collect_once(conn: sqlite3.Connection, adapters: Iterable[ProviderAdapter],
                 quiet: bool = False) -> dict[str, bool]:
    """Collect providers sequentially, isolating fetch/parse failures per provider."""
    results = {}
    for adapter in adapters:
        results[adapter.provider_key] = collect_provider(conn, adapter, quiet=quiet)
    return results


def run(database: pathlib.Path, interval_seconds: int = 60, once: bool = False,
        quiet: bool = False, adapters: Iterable[ProviderAdapter] = DEFAULT_ADAPTERS) -> int:
    if interval_seconds < 1:
        raise ValueError("Polling interval must be at least one second")
    adapters = tuple(adapters)
    conn = open_database(database)
    if not quiet:
        print(f"Database: {database.expanduser().resolve()}")
        print(f"Providers: {', '.join(adapter.display_name for adapter in adapters)}")
        print(f"Polling: every {interval_seconds} seconds; Ctrl+C to stop.", flush=True)
    try:
        while True:
            started = time.monotonic()
            collect_once(conn, adapters, quiet=quiet)
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
