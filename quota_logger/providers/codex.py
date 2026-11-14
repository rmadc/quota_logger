"""Codex quota API adapter."""
from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request

from .base import (QuotaWindow, first_dict, first_value, numeric_percentage,
                   positive_duration, reset_epoch)


URL = "https://chatgpt.com/backend-api/wham/usage"
AUTH_PATH = pathlib.Path.home() / ".codex" / "auth.json"


class CodexAdapter:
    provider_key = "codex"
    display_name = "Codex"

    def __init__(self, auth_path: pathlib.Path | str | None = None):
        self.auth_path = pathlib.Path(auth_path) if auth_path is not None else AUTH_PATH

    def read_access_token(self) -> tuple[str, str | None]:
        try:
            auth = json.loads(self.auth_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Codex credentials not found at {self.auth_path}; sign into Codex first"
            ) from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Could not read Codex credentials: {exc}") from exc
        if not isinstance(auth, dict):
            raise RuntimeError("Codex credentials file did not contain a JSON object")
        tokens = auth.get("tokens") or {}
        access = tokens.get("access_token") if isinstance(tokens, dict) else None
        access = access or auth.get("access_token")
        if not access:
            raise RuntimeError("No Codex access token found; sign into Codex first")
        account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
        account_id = account_id or auth.get("account_id")
        return str(access), None if account_id is None else str(account_id)

    def fetch_usage(self) -> dict:
        access, account_id = self.read_access_token()
        headers = {
            "Authorization": f"Bearer {access}",
            "Accept": "application/json",
            "User-Agent": "CodexQuotaHistory/1.0",
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        request = urllib.request.Request(URL, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Codex usage request failed (HTTP {exc.code})") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Codex usage request failed: {exc.reason}") from None
        if not isinstance(payload, dict):
            raise ValueError("Codex usage response was not a JSON object")
        return payload

    def parse_response(self, payload: dict) -> list[QuotaWindow]:
        if not isinstance(payload, dict):
            raise ValueError("Codex usage response was not a JSON object")
        rate = first_dict(payload.get("rate_limit"), payload.get("rate_limits"),
                          payload.get("rateLimits")) or {}
        windows = []
        for keys, fallback_key, fallback_label, fallback_duration in (
            (("five_hour", "primary_window", "primary"), "five_hour", "5h", 5 * 3600),
            (("weekly", "secondary_window", "secondary"), "weekly", "7D", 7 * 86400),
        ):
            item = first_dict(*(rate.get(key) for key in keys))
            if item is None:
                continue
            duration = positive_duration(first_value(item, "limit_window_seconds"))
            if duration is not None:
                key, label = (("weekly", "7D") if duration >= 172800
                              else ("five_hour", "5h"))
            else:
                key, label = fallback_key, fallback_label
                duration = fallback_duration
            used = numeric_percentage(
                first_value(item, "used_percent", "usedPercent"), "Codex", label
            )
            reset = reset_epoch(first_value(item, "reset_at", "resetsAt", "reset_time_ms"))
            if used is None and reset is None:
                continue
            windows.append(QuotaWindow(key, label, used, reset, duration))
        if not windows:
            raise ValueError("No recognizable Codex quota window in response")
        return windows
