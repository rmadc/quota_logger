"""Cute text-mode status and controls for the unified quota collector task."""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import os
import pathlib
import sqlite3
import sys
from contextlib import closing
from ctypes import wintypes

from .collector import DEFAULT_DATABASE
from .display import local_time
from .history import connect_readonly, display_database_path
from . import task_manager


_ANSI = False
_PALETTE = {
    "title": "96;1", "accent": "96", "good": "92", "warn": "93",
    "bad": "91", "muted": "90", "plain": "97",
}


def enable_colors() -> None:
    global _ANSI
    if os.name != "nt":
        _ANSI = True
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_handle = kernel32.GetStdHandle
        get_handle.argtypes = [wintypes.DWORD]
        get_handle.restype = wintypes.HANDLE
        get_mode = kernel32.GetConsoleMode
        get_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_mode.restype = wintypes.BOOL
        set_mode = kernel32.SetConsoleMode
        set_mode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        set_mode.restype = wintypes.BOOL
        handle = get_handle(0xFFFFFFF5)
        mode = wintypes.DWORD()
        _ANSI = bool(get_mode(handle, ctypes.byref(mode)) and
                     set_mode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        _ANSI = False


def paint(text: str, color: str = "plain") -> str:
    if not _ANSI:
        return str(text)
    return f"\x1b[{_PALETTE.get(color, _PALETTE['plain'])}m{text}\x1b[0m"


def say(text: str = "", color: str = "plain") -> None:
    print(paint(text, color))


def banner() -> None:
    say("       /\\_/\\", "accent")
    say("      ( o.o )   QUOTA LOGGER", "title")
    say("       > ^ <    CONFIG CONSOLE", "accent")


def provider_statuses(database: pathlib.Path = DEFAULT_DATABASE) -> list[tuple]:
    if not database.is_file():
        return []
    with closing(connect_readonly(database)) as conn:
        providers = conn.execute("""
            SELECT provider_id, provider_key, display_name
            FROM providers ORDER BY display_name COLLATE NOCASE
        """).fetchall()
        results = []
        for provider_id, key, name in providers:
            row = conn.execute("""
                SELECT sample_id, observed_at_utc, ok, error
                FROM samples WHERE provider_id = ?
                ORDER BY sample_id DESC LIMIT 1
            """, (provider_id,)).fetchone()
            if not row:
                results.append((key, name, None, None, None, None, []))
                continue
            sample_id, stamp, ok, error = row
            windows = conn.execute("""
                SELECT window_label, used_pct FROM quota_windows
                WHERE sample_id = ? ORDER BY window_key
            """, (sample_id,)).fetchall()
            results.append((key, name, stamp, bool(ok), error, sample_id, windows))
        return results


def show_database_status(database: pathlib.Path = DEFAULT_DATABASE) -> None:
    say(f"Database: {display_database_path(database)}", "muted")
    if not database.is_file():
        say("No shared history database yet.", "warn")
        return
    try:
        statuses = provider_statuses(database)
    except (OSError, sqlite3.Error) as exc:
        say(f"History database unavailable: {exc}", "bad")
        return
    if not statuses:
        say("No provider records yet.", "warn")
    for _key, name, stamp, ok, error, _sample_id, windows in statuses:
        if stamp is None:
            say(f"{name}: waiting for first sample", "muted")
            continue
        local_stamp = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
        if ok:
            usage = "  ".join(
                f"{label}: {'-' if value is None else f'{value:g}%'}"
                for label, value in windows
            )
            say(f"{name}: {local_stamp:%d/%m/%y} {local_time(stamp)}  {usage or 'sample saved'}",
                "good")
        else:
            say(f"{name}: {local_stamp:%d/%m/%y} {local_time(stamp)}  ERROR: {error or 'unknown'}",
                "bad")


def show_status(database: pathlib.Path = DEFAULT_DATABASE) -> str | None:
    try:
        state = task_manager.status()
        if state == "MISSING":
            say("Background logger: not installed", "warn")
        else:
            color = "good" if state == "Running" else "warn" if state == "Stopped" else "accent"
            say(f"Background logger task: {state}", color)
    except Exception as exc:
        say(f"Could not check the background task: {exc}", "bad")
        state = None
    show_database_status(database)
    return state


def run_action(action: str, database: pathlib.Path = DEFAULT_DATABASE) -> None:
    if action == "install":
        if task_manager.status() == "Running":
            task_manager.stop()
        task_manager.register(database)
        task_manager.start()
    elif action == "start":
        task_manager.start()
    elif action == "stop":
        task_manager.stop()
    elif action == "restart":
        task_manager.restart()
    elif action == "uninstall":
        task_manager.unregister()
    else:
        raise ValueError(f"Unknown action: {action}")


def confirm_install() -> bool:
    answer = input(paint(
        "Install the per-user logon task and start collection? [y/N] ", "warn"
    )).strip().lower()
    return answer in {"y", "yes"}


def app_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DATABASE)
    args = parser.parse_args(argv)
    enable_colors()
    try:
        current = task_manager.status()
    except Exception as exc:
        current = None
        banner()
        say(f"Task Scheduler status unavailable: {exc}", "bad")

    if current == "MISSING":
        banner()
        say("The unified background task is not installed yet.", "warn")
        if confirm_install():
            try:
                run_action("install", args.db)
                say("Logon task installed and started. Nice and tidy!", "good")
            except Exception as exc:
                say(f"Task setup failed: {exc}", "bad")

    while True:
        say("\n" + "=" * 66, "muted")
        banner()
        show_status(args.db)
        say("\n[I]nstall/update  [S]tart  St[o]p  [R]estart  [U]ninstall  [Q]uit", "title")
        choice = input(paint("  What shall we do? > ", "accent")).strip().lower()
        if choice in {"q", "quit", ""}:
            return 0
        action = {
            "i": "install", "install": "install", "s": "start", "start": "start",
            "o": "stop", "stop": "stop", "r": "restart", "restart": "restart",
            "u": "uninstall", "uninstall": "uninstall",
        }.get(choice)
        if not action:
            say("Choose install, start, stop, restart, uninstall, or quit.", "warn")
            continue
        if action in {"install", "uninstall"}:
            answer = input(paint(f"Confirm {action} the unified task? [y/N] ", "warn"))
            if answer.strip().lower() not in {"y", "yes"}:
                say("No changes made.", "muted")
                continue
        try:
            run_action(action, args.db)
            say(f"Task {action} completed. All set!", "good")
            if action == "uninstall":
                return 0
        except Exception as exc:
            say(f"Task {action} failed: {exc}", "bad")


if __name__ == "__main__":
    raise SystemExit(app_main())
