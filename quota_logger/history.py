"""Side-by-side, provider-neutral history viewer for the shared quota database."""
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
from .display import (ansi_color, date_separator, fit_console_width, local_time,
                      quota_pressure, reset_countdown, weekly_pressure)


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
FALLBACK_WINDOWS = {
    "codex": [("five_hour", "5h", 18_000), ("weekly", "7D", 604_800)],
    "claude": [("five_hour", "5h", 18_000), ("weekly", "7D", 604_800)],
}


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
        FROM quota_windows GROUP BY window_key, window_label
    """).fetchall()
    preferred = {"five_hour": 0, "weekly": 1}
    rows.sort(key=lambda row: (preferred.get(row[0], 2), row[0], row[1]))
    definitions = []
    seen = set()
    for key, label, duration in rows:
        if key not in seen:
            seen.add(key)
            definitions.append(WindowDefinition(key, label, duration))
    return definitions


def provider_definitions(conn: sqlite3.Connection):
    providers = conn.execute("""
        SELECT p.provider_id, p.provider_key, p.display_name
        FROM providers p WHERE EXISTS (
            SELECT 1 FROM samples s WHERE s.provider_id = p.provider_id
        ) ORDER BY p.provider_id
    """).fetchall()
    all_windows = window_definitions(conn)
    result = []
    for provider_id, key, name in providers:
        rows = conn.execute("""
            SELECT window_key, MAX(window_label), MAX(window_seconds)
            FROM quota_windows JOIN samples USING(sample_id)
            WHERE provider_id = ? GROUP BY window_key
        """, (provider_id,)).fetchall()
        canonical_labels = {"five_hour": "5h", "weekly": "7D"}
        windows = [WindowDefinition(key, canonical_labels.get(key, label), duration)
                   for key, label, duration in rows]
        windows.sort(key=lambda item: ({"five_hour": 0, "weekly": 1}.get(item.key, 2), item.key))
        if key in FALLBACK_WINDOWS:
            existing = {item.key for item in windows}
            fallback = [WindowDefinition(*item) for item in FALLBACK_WINDOWS[key]
                        if item[0] not in existing]
            windows = fallback + windows
            windows.sort(key=lambda item: ({"five_hour": 0, "weekly": 1}.get(item.key, 2), item.key))
        result.append((provider_id, key, name, windows))
    return result


def fetch_samples(conn: sqlite3.Connection, limit: int | None = None,
                  after_id: int | None = None, include_errors: bool = False,
                  provider_id: int | None = None) -> list[HistorySample]:
    conditions = []
    params: list[object] = []
    if after_id is not None:
        conditions.append("s.sample_id > ?")
        params.append(after_id)
    if not include_errors:
        conditions.append("s.ok = 1")
    if provider_id is not None:
        conditions.append("s.provider_id = ?")
        params.append(provider_id)
    if limit is not None and after_id is None:
        if provider_id is None:
            conditions.append("s.sample_id IN (SELECT sample_id FROM samples "
                              "ORDER BY observed_at_utc DESC, sample_id DESC LIMIT ?)")
            params.append(limit)
        else:
            conditions.append("s.sample_id IN (SELECT sample_id FROM samples WHERE provider_id = ? "
                              "ORDER BY observed_at_utc DESC, sample_id DESC LIMIT ?)")
            params.extend((provider_id, limit))
    clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(f"""
        SELECT s.sample_id, s.provider_id, p.provider_key, p.display_name,
               s.observed_at_utc, s.ok, s.error, w.window_key, w.window_label,
               w.used_pct, w.reset_at_epoch, w.window_seconds
        FROM samples s JOIN providers p USING(provider_id)
        LEFT JOIN quota_windows w USING(sample_id) {clause}
        ORDER BY s.observed_at_utc, s.sample_id, w.window_key
    """, params).fetchall()
    grouped = {}
    for row in rows:
        sid, pid, key, name, stamp, ok, error, wkey, label, used, reset, duration = row
        sample = grouped.setdefault(sid, HistorySample(sid, pid, key, name, stamp, bool(ok), error))
        if wkey is not None:
            sample.windows[wkey] = (label, used, reset, duration)
    return list(grouped.values())


def latest_sample_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(sample_id), 0) FROM samples").fetchone()[0]


def provider_window_history(conn, sample, window_key, reset, sample_epoch):
    start = dt.datetime.fromtimestamp(sample_epoch - 3 * 3600, dt.timezone.utc).isoformat()
    rows = conn.execute("""
        SELECT s.sample_id, s.observed_at_utc, w.used_pct, w.reset_at_epoch
        FROM samples s JOIN quota_windows w USING(sample_id)
        WHERE s.provider_id = ? AND s.ok = 1 AND w.window_key = ?
          AND w.reset_at_epoch = ? AND s.sample_id <= ?
          AND julianday(s.observed_at_utc) >= julianday(?)
        ORDER BY s.sample_id
    """, (sample.provider_id, window_key, reset, sample.sample_id, start)).fetchall()
    return [(sid, dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp(), pct, epoch)
            for sid, stamp, pct, epoch in rows]


def _window_pressure(conn, sample, definition, value, reset, duration):
    reset = _effective_reset(sample, value, reset, duration)
    if definition.key == "weekly":
        history = ()
        if reset is not None:
            parsed = dt.datetime.fromisoformat(sample.observed_at_utc.replace("Z", "+00:00"))
            history = provider_window_history(conn, sample, definition.key, reset, parsed.timestamp())
        return weekly_pressure(value, reset, sample.observed_at_utc, duration,
                               history=history, sample_id=sample.sample_id)
    return quota_pressure(value, reset, sample.observed_at_utc, duration)


def _effective_reset(sample, used, reset, duration):
    if reset is None and used == 0 and duration:
        observed = dt.datetime.fromisoformat(sample.observed_at_utc.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=dt.timezone.utc)
        reset = observed.timestamp() + duration
    return reset


def _window_eta(sample, used, reset, duration):
    reset = _effective_reset(sample, used, reset, duration)
    return reset_countdown(reset, sample.observed_at_utc, duration)


def _sample_day(sample):
    value = dt.datetime.fromisoformat(sample.observed_at_utc.replace("Z", "+00:00"))
    return value.astimezone().date().isoformat()


def _visible_width(value):
    return len(ANSI_RE.sub("", value))


def pane_header(windows):
    if [w.key for w in windows] == ["five_hour", "weekly"]:
        return f'{"Time":5}{"5h":>5}{"7D":>5} {"5h ETA":>8} {"7D ETA":>7}', 32
    fields = [("Time", 5, False)]
    fields.extend((w.label, max(5, len(w.label)), True) for w in windows)
    fields.extend((f"{w.label} ETA", max(7, len(w.label) + 4), True) for w in windows)
    header = " ".join((label.rjust(width) if right else label.ljust(width))
                      for label, width, right in fields)
    return header, len(header)


def pane_row(conn, sample, windows, color):
    values = [local_time(sample.observed_at_utc)]
    for definition in windows:
        current = sample.windows.get(definition.key)
        if current is None:
            values.extend(("-", "-"))
            continue
        _label, used, reset, duration = current
        pct = "-" if used is None else f"{used:g}%"
        pressure = _window_pressure(conn, sample, definition, used, reset, duration)
        eta = _window_eta(sample, used, reset, duration)
        values.extend((ansi_color(pct, pressure, color), eta))
    if not sample.ok:
        values[1] = ansi_color("ERR", 2.0, color) if color else "ERR"
    if len(windows) == 2 and [w.key for w in windows] == ["five_hour", "weekly"]:
        widths = (5, 5, 5, 8, 7)
    else:
        widths = ((5,) + tuple(max(5, len(w.label)) for w in windows)
                  + tuple(max(7, len(w.label) + 4) for w in windows))
    # The classic Codex pane puts both quota readings before their ETA fields.
    cells = [values[0]]
    cells.extend(values[1::2])
    cells.extend(values[2::2])
    chunks = []
    for index, (value, width) in enumerate(zip(cells, widths)):
        visible = _visible_width(value)
        if index == 0:
            padded = value + " " * max(0, width - visible)
        elif index >= 3:
            padded = " " + " " * max(0, width - visible) + value
        else:
            padded = " " * max(0, width - visible) + value
        chunks.append(padded)
    line = "".join(chunks)
    return line + " " * max(0, pane_header(windows)[1] - _visible_width(line))


def _changed_samples(samples):
    """Keep first success and samples whose displayed quota percentages changed."""
    shown = []
    previous = None
    for sample in samples:
        if not sample.ok:
            shown.append(sample)
            continue
        current = tuple((key, value[1]) for key, value in sorted(sample.windows.items()))
        if previous is None or current != previous:
            shown.append(sample)
        previous = current
    return shown


def _quota_values(sample):
    return tuple((key, value[1]) for key, value in sorted(sample.windows.items()))


def _pane_lines(conn, samples, windows, color):
    lines = []
    last_day = None
    for sample in samples:
        day = _sample_day(sample)
        if day != last_day:
            lines.append(date_separator(day, pane_header(windows)[1], color))
            last_day = day
        lines.append(pane_row(conn, sample, windows, color))
    return lines


def _pad(value, width):
    return value + " " * max(0, width - _visible_width(value))


def _paired(left, right, widths):
    return _pad(left, widths[0]) + " | " + _pad(right, widths[1])


def _aligned_rows(panes, widths):
    count = min(map(len, panes))
    if not count:
        return []
    return [_paired(left, right, widths)
            for left, right in zip(panes[0][-count:], panes[1][-count:])]


def _update_rows(previous, current):
    """Rewrite only the changed suffix in the normal console scrollback buffer."""
    first = 0
    while first < min(len(previous), len(current)) and previous[first] == current[first]:
        first += 1
    if first == len(current) == len(previous):
        return
    if not sys.stdout.isatty():
        print("\n".join(current), flush=True)
        return
    sys.stdout.flush()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Coord(ctypes.Structure):
            _fields_ = [("x", wintypes.SHORT), ("y", wintypes.SHORT)]

        class Rect(ctypes.Structure):
            _fields_ = [("left", wintypes.SHORT), ("top", wintypes.SHORT),
                        ("right", wintypes.SHORT), ("bottom", wintypes.SHORT)]

        class Info(ctypes.Structure):
            _fields_ = [("size", Coord), ("cursor", Coord),
                        ("attributes", wintypes.WORD), ("window", Rect),
                        ("maximum", Coord)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel.GetStdHandle(-11)
        kernel.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
        kernel.SetConsoleCursorPosition.argtypes = [wintypes.HANDLE, Coord]
        info = Info()
        if not kernel.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            raise OSError("Cannot read History console position")
        target = info.cursor.y - len(previous) + first
        if target < 0:
            # The oldest rows have fallen out of the console buffer.
            first -= target
            target = 0
        if not kernel.SetConsoleCursorPosition(handle, Coord(0, target)):
            raise OSError("Cannot position History console cursor")
    else:
        sys.stdout.write(f"\x1b[{len(previous) - first}F")
    sys.stdout.write("\n".join(current[first:]) + "\n")
    sys.stdout.flush()


def print_paired(providers, panes, widths):
    print(_paired(providers[0][2].upper(), providers[1][2].upper(), widths))
    print(_paired(pane_header(providers[0][3])[0], pane_header(providers[1][3])[0], widths))
    print("-" * widths[0] + "-+-" + "-" * widths[1])
    count = min(map(len, panes))
    if not count:
        print("No quota history changes yet.")
        return
    for left, right in zip(panes[0][-count:], panes[1][-count:]):
        print(_paired(left, right, widths))


def display_database_path(path: pathlib.Path) -> str:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        try:
            relative = path.resolve().relative_to(pathlib.Path(local_app_data).resolve())
            return "%LOCALAPPDATA%\\" + str(relative)
        except ValueError:
            pass
    return str(path.resolve())


def ensure_legend_encoding():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            "◆".encode(sys.stdout.encoding or "utf-8")
        except (LookupError, UnicodeEncodeError):
            sys.stdout.reconfigure(encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample_count", type=int, nargs="?", default=None)
    parser.add_argument("-N", "--count", dest="count", type=int)
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DATABASE)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--no-follow", dest="follow", action="store_false",
                        help="Show the current history and exit")
    parser.set_defaults(follow=False)
    parser.add_argument("--errors", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    limit = args.count if args.count is not None else (args.sample_count or 100)
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
        providers = provider_definitions(conn)
        last_id = latest_sample_id(conn)
        all_samples = [fetch_samples(conn, limit=None, include_errors=args.errors,
                                     provider_id=provider[0]) for provider in providers]
        panes = [_pane_lines(conn, _changed_samples(samples)[-limit:], provider[3], color)
                 for provider, samples in zip(providers, all_samples)]
        last_days = [(_sample_day(samples[-1]) if samples else None) for samples in all_samples]
        last_values = {}
        for provider, samples in zip(providers, all_samples):
            successful = [sample for sample in samples if sample.ok]
            if successful:
                last_values[provider[0]] = _quota_values(successful[-1])
        conn.commit()
        database_line = f"Database: {display_database_path(args.db)}"
        pane_widths = [pane_header(provider[3])[1] for provider in providers]
        output_width = (sum(pane_widths) + 3 * (len(pane_widths) - 1)
                        if len(providers) == 2 else max(pane_widths, default=0))
        fit_console_width(max(output_width, len(database_line)))
        print(database_line)
        if len(providers) == 2:
            widths = [pane_header(p[3])[1] for p in providers]
            print_paired(providers, panes, widths)
        else:
            for provider, lines in zip(providers, panes):
                print(provider[2].upper())
                print(pane_header(provider[3])[0])
                print("-" * pane_header(provider[3])[1])
                for line in lines[-limit:]:
                    print(line)
        if not args.follow:
            return 0
        previous_rows = _aligned_rows(panes, widths) if len(providers) == 2 else []
        while True:
            time.sleep(2)
            conn.execute("BEGIN")
            try:
                new = fetch_samples(conn, after_id=last_id, include_errors=args.errors)
                last_id = latest_sample_id(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            changed = False
            for sample in new:
                index = next((i for i, p in enumerate(providers)
                              if p[0] == sample.provider_id), None)
                if index is None:
                    continue
                if sample.ok:
                    values = _quota_values(sample)
                    if last_values.get(sample.provider_id) == values:
                        continue
                    last_values[sample.provider_id] = values
                day = _sample_day(sample)
                if last_days[index] != day:
                    panes[index].append(date_separator(day, pane_widths[index], color))
                    last_days[index] = day
                panes[index].append(pane_row(conn, sample, providers[index][3], color))
                changed = True
            if changed and len(providers) == 2:
                rows = _aligned_rows(panes, widths)
                _update_rows(previous_rows, rows)
                previous_rows = rows
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
