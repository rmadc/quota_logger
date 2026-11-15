"""Compact shared quota formatting and the Codex method-1 pace colors."""
from __future__ import annotations

import datetime as dt
import ctypes
import os
import sys
from ctypes import wintypes
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


COLOR_STOPS = (
    (0.0, (0, 140, 255)),
    (0.5, (0, 220, 70)),
    (1.0, (255, 255, 0)),
    (1.5, (255, 128, 0)),
    (2.0, (255, 0, 0)),
)
RESET = "\x1b[0m"
WEEKLY_SECONDS = 7 * 24 * 3600
ACTIVE_START_HOUR = 8
ACTIVE_END_HOUR = 20
EARLY_CUSHION_HOURS = 3.0
WEEKLY_LOOKBACK_MINUTES = 120
WEEKLY_RECENT_WEIGHT = 0.25
WEEKLY_MIN_BASELINE = 0.80
SEPARATOR_BRIGHTNESS = 0.70

try:
    LOCAL_ZONE = ZoneInfo("Europe/Madrid")
except ZoneInfoNotFoundError:
    LOCAL_ZONE = None


def pressure_rgb(pressure: float, brightness: float = 1.0) -> tuple[int, int, int]:
    pressure = min(2.0, max(0.0, float(pressure)))
    if 0.9 <= pressure <= 1.1:
        return tuple(round(255 * brightness) for _ in range(3))
    for (low, start), (high, finish) in zip(COLOR_STOPS, COLOR_STOPS[1:]):
        if pressure <= high:
            fraction = (pressure - low) / (high - low)
            return tuple(round((a + (b - a) * fraction) * brightness)
                         for a, b in zip(start, finish))
    return tuple(round(value * brightness) for value in COLOR_STOPS[-1][1])


def ansi_color(text: str, pressure: float | None, enabled: bool) -> str:
    if not enabled or pressure is None:
        return text
    red, green, blue = pressure_rgb(pressure)
    return f"\x1b[38;2;{red};{green};{blue}m{text}{RESET}"


def _madrid_bounds_fallback(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    march_end = dt.date(day.year, 3, 31)
    october_end = dt.date(day.year, 10, 31)
    march_sunday = march_end - dt.timedelta(days=(march_end.weekday() + 1) % 7)
    october_sunday = october_end - dt.timedelta(days=(october_end.weekday() + 1) % 7)
    offset = dt.timedelta(hours=2 if march_sunday <= day < october_sunday else 1)
    opening = dt.datetime.combine(day, dt.time(ACTIVE_START_HOUR), dt.timezone.utc) - offset
    closing = dt.datetime.combine(day, dt.time(ACTIVE_END_HOUR), dt.timezone.utc) - offset
    return opening, closing


def _madrid_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    if LOCAL_ZONE is not None:
        opening = dt.datetime.combine(day, dt.time(ACTIVE_START_HOUR), LOCAL_ZONE)
        closing = dt.datetime.combine(day, dt.time(ACTIVE_END_HOUR), LOCAL_ZONE)
        return opening.astimezone(dt.timezone.utc), closing.astimezone(dt.timezone.utc)
    return _madrid_bounds_fallback(day)


def active_hours(start: dt.datetime, end: dt.datetime) -> float:
    start = start.astimezone(dt.timezone.utc)
    end = end.astimezone(dt.timezone.utc)
    if end <= start:
        return 0.0
    day = start.date() - dt.timedelta(days=1)
    last_day = end.date() + dt.timedelta(days=1)
    total = 0.0
    while day <= last_day:
        opening, closing = _madrid_bounds(day)
        left, right = max(opening, start), min(closing, end)
        if right > left:
            total += (right - left).total_seconds() / 3600.0
        day += dt.timedelta(days=1)
    return total


def quota_pressure(used_pct, reset_epoch, sample_time: str, window_seconds):
    if used_pct is None or reset_epoch is None or not window_seconds:
        return None
    try:
        sample = dt.datetime.fromisoformat(sample_time.replace("Z", "+00:00"))
        if sample.tzinfo is None:
            sample = sample.replace(tzinfo=dt.timezone.utc)
        remaining = float(reset_epoch) - sample.timestamp()
        elapsed = 1.0 - max(0.0, remaining) / float(window_seconds)
        if elapsed <= 0.0:
            return 0.0 if float(used_pct) <= 0.0 else 2.0
        return max(0.0, float(used_pct)) / (100.0 * elapsed)
    except (ValueError, TypeError, OverflowError):
        return None


def _recent_rate(history, sample_id, sample_epoch, reset_epoch):
    window_start = sample_epoch - WEEKLY_LOOKBACK_MINUTES * 60
    current = None
    first_in_window = None
    baseline = None
    for sample in reversed(history):
        sid, epoch, pct, reset = sample
        if sid > sample_id or epoch > sample_epoch:
            continue
        if reset != reset_epoch or pct is None:
            if epoch < window_start:
                break
            continue
        if current is None:
            current = sample
        if epoch >= window_start:
            first_in_window = sample
        else:
            baseline = sample
            break
    if current is None:
        return None
    reference = baseline or first_in_window
    if reference is None:
        return None
    elapsed_hours = (current[1] - reference[1]) / 3600.0
    if elapsed_hours < 3.0 / 60.0:
        return None
    difference = float(current[2]) - float(reference[2])
    if difference < 0:
        return None
    return difference / elapsed_hours


def weekly_pressure(used_pct, reset_epoch, sample_time: str, window_seconds,
                    history=(), sample_id=None):
    """Codex method-1 weekly pace model, generalized to the window duration."""
    if used_pct is None or reset_epoch is None or not window_seconds:
        return None
    try:
        used = min(100.0, max(0.0, float(used_pct)))
        if used >= 100.0:
            return 2.0
        reset = dt.datetime.fromtimestamp(float(reset_epoch), dt.timezone.utc)
        sample = dt.datetime.fromisoformat(sample_time.replace("Z", "+00:00"))
        if sample.tzinfo is None:
            sample = sample.replace(tzinfo=dt.timezone.utc)
        sample = sample.astimezone(dt.timezone.utc)
        begin = reset - dt.timedelta(seconds=int(window_seconds))
        if not begin <= sample <= reset:
            return quota_pressure(used_pct, reset_epoch, sample_time, window_seconds)
        total = active_hours(begin, reset)
        elapsed = active_hours(begin, sample)
        remaining = active_hours(sample, reset)
        if total <= 0.0:
            return None
        elapsed_effective = min(total, elapsed + EARLY_CUSHION_HOURS)
        cumulative_rate = used / elapsed_effective if elapsed_effective else 0.0
        budget_pressure = (used * total / (100.0 * elapsed_effective)
                           if used else 0.0)
        budget_warning = budget_pressure * min(1.0, remaining / 8.0)
        burst_rate = None
        if sample_id is not None and history:
            burst_rate = _recent_rate(history, sample_id, sample.timestamp(), reset_epoch)
        if burst_rate is None:
            forecast_rate = cumulative_rate
        else:
            forecast_rate = max(
                WEEKLY_MIN_BASELINE * cumulative_rate,
                (1.0 - WEEKLY_RECENT_WEIGHT) * cumulative_rate
                + WEEKLY_RECENT_WEIGHT * burst_rate,
            )
        demand = remaining * forecast_rate
        available = 100.0 - used
        if available <= 0.0:
            demand_pressure = 2.0 if demand > 0.0 else 0.0
        else:
            ratio = demand / available
            anchors = ((0.0, 0.0), (0.25, 0.5), (0.60, 1.0),
                       (1.0, 1.5), (1.5, 2.0))
            for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
                if ratio <= x1:
                    demand_pressure = y0 + (y1 - y0) * (ratio - x0) / (x1 - x0)
                    break
            else:
                demand_pressure = 2.0
        return max(budget_warning, demand_pressure)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def local_time(iso_timestamp: str) -> str:
    value = dt.datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    return value.astimezone().strftime("%H:%M")


def reset_countdown(reset_epoch, sample_time: str, window_seconds: int | None) -> str:
    if reset_epoch is None:
        return "-"
    try:
        sample = dt.datetime.fromisoformat(sample_time.replace("Z", "+00:00"))
        if sample.tzinfo is None:
            sample = sample.replace(tzinfo=dt.timezone.utc)
        seconds = max(0, int(float(reset_epoch) - sample.timestamp()))
        minutes = (seconds + 59) // 60
        if window_seconds is not None and window_seconds >= 24 * 3600:
            days, remainder = divmod(minutes, 24 * 60)
            hours = (remainder + 59) // 60
            if hours == 24:
                days, hours = days + 1, 0
            return f"{days:02d}d:{hours:02d}h"
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}h:{minutes:02d}m"
    except (OverflowError, TypeError, ValueError):
        return "?"


def date_separator(day: str, width: int, enabled: bool) -> str:
    width = max(width, 32)
    chars = list("-" * width)
    date_label = f"◆ {dt.date.fromisoformat(day).strftime('%d/%m/%y')} ◆"
    center = width // 2
    date_start = center - len(date_label) // 2
    if width <= 36:
        positions = (0, 6, date_start, date_start + len(date_label), width - 2)
    else:
        positions = (0, round(width * 0.16), date_start,
                     round(width * 0.68), width - 2)
    for label, position in zip(("0x", "0.5x", date_label, "1.5x", "2x"), positions):
        chars[position:position + len(label)] = list(label)
    line = "".join(chars)
    if not enabled:
        return line
    pieces = []
    for index, char in enumerate(line):
        pressure = (index / max(1, center) if index <= center else
                    1.0 + (index - center) / max(1, width - center - 1))
        red, green, blue = pressure_rgb(pressure, SEPARATOR_BRIGHTNESS)
        pieces.append(f"\x1b[38;2;{red};{green};{blue}m{char}")
    return "".join(pieces) + RESET


def fit_console_width(requested: int) -> None:
    """Narrow a Windows console to the rendered history width when possible."""
    if os.name != "nt" or requested < 1:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
                        ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT)]

        class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
            _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                        ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT),
                        ("dwMaximumWindowSize", COORD)]

        get_handle = kernel32.GetStdHandle
        get_handle.argtypes = [wintypes.DWORD]
        get_handle.restype = wintypes.HANDLE
        get_info = kernel32.GetConsoleScreenBufferInfo
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]
        get_info.restype = wintypes.BOOL
        set_window = kernel32.SetConsoleWindowInfo
        set_window.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(SMALL_RECT)]
        set_window.restype = wintypes.BOOL
        set_buffer = kernel32.SetConsoleScreenBufferSize
        set_buffer.argtypes = [wintypes.HANDLE, COORD]
        set_buffer.restype = wintypes.BOOL

        handle = get_handle(0xFFFFFFF5)  # STD_OUTPUT_HANDLE (-11)
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not get_info(handle, ctypes.byref(info)):
            return
        target = min(requested, info.dwSize.X)
        current = info.srWindow.Right - info.srWindow.Left + 1
        height = info.srWindow.Bottom - info.srWindow.Top + 1
        if target < 1:
            return
        if target < current:
            rect = SMALL_RECT(0, info.srWindow.Top, target - 1, info.srWindow.Bottom)
            set_window(handle, True, ctypes.byref(rect))
        if target < info.dwSize.X:
            set_buffer(handle, COORD(target, info.dwSize.Y))
        sys.stdout.write(f"\x1b[8;{height};{target}t")
        sys.stdout.flush()
    except (AttributeError, OSError, ValueError, ctypes.ArgumentError):
        return
