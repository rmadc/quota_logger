"""Claude Code OAuth usage API adapter."""
from __future__ import annotations

import json
import datetime as dt
import email.utils
import hashlib
import os
import pathlib
import subprocess
import urllib.error
import urllib.request

from .base import QuotaWindow, first_value, numeric_percentage, reset_epoch


URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
FALLBACK_USER_AGENT = "claude-code/2.1.204"


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ClaudeAuthenticationError(RuntimeError):
    def __init__(self, token, refresh_attempted=False):
        self.token_fingerprint = token_fingerprint(token)
        super().__init__("Claude usage request failed (HTTP 401); sign in again; "
                         f"token_fingerprint={self.token_fingerprint}; "
                         f"refresh_attempted={int(refresh_attempted)}")


class ClaudeRateLimitError(RuntimeError):
    def __init__(self, retry_after):
        now = dt.datetime.now(dt.timezone.utc)
        try:
            value = (retry_after or "").strip()
            if value.isascii() and value.isdigit():
                seconds = int(value)
                deadline = now + dt.timedelta(seconds=seconds)
            else:
                deadline = email.utils.parsedate_to_datetime(value)
                if deadline is None or deadline.tzinfo is None:
                    raise ValueError("Invalid Retry-After date")
        except (ValueError, TypeError, OverflowError):
            deadline = now + dt.timedelta(minutes=5)
        self.retry_at = max(now.timestamp(), deadline.timestamp())
        header = "" if retry_after is None else f"; retry_after={json.dumps(retry_after)}"
        super().__init__(f"Claude usage request failed (HTTP 429); retry_at={self.retry_at:.3f}{header}")


def default_credentials_path() -> pathlib.Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = pathlib.Path(config_dir).expanduser() if config_dir else pathlib.Path.home() / ".claude"
    return root / ".credentials.json"


class ClaudeAdapter:
    provider_key = "claude"
    display_name = "Claude"

    def __init__(self, credentials_path: pathlib.Path | str | None = None,
                 user_agent: str | None = None):
        self.credentials_path = (pathlib.Path(credentials_path) if credentials_path is not None
                                 else default_credentials_path())
        self._user_agent = user_agent
        self.refresh_event = None

    def read_access_token(self) -> str:
        try:
            credentials = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Claude Code credentials not found at {self.credentials_path}; sign into Claude Code first"
            ) from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Could not read Claude Code credentials: {exc}") from exc
        oauth = credentials.get("claudeAiOauth") if isinstance(credentials, dict) else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        if not token:
            raise RuntimeError("No Claude Code OAuth token found; sign into Claude Code first")
        return str(token)

    def user_agent(self) -> str:
        if self._user_agent:
            return self._user_agent
        try:
            result = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True,
                timeout=3, check=False,
            )
            version = (result.stdout or result.stderr).strip().split()
            if result.returncode == 0 and version:
                self._user_agent = f"claude-code/{version[-1]}"
        except (OSError, subprocess.SubprocessError):
            pass
        self._user_agent = self._user_agent or FALLBACK_USER_AGENT
        return self._user_agent

    def current_token_fingerprint(self) -> str | None:
        try:
            return token_fingerprint(self.read_access_token())
        except RuntimeError:
            return None

    def try_cli_refresh(self, rejected_token: str) -> str | None:
        """Ask Claude Code to refresh its own credentials at most once."""
        try:
            current = self.read_access_token()
            if current != rejected_token:
                return current
            env = os.environ.copy()
            env["CLAUDE_CONFIG_DIR"] = str(self.credentials_path.parent)
            subprocess.run(["claude", "-p", "/usage"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60, check=False, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass
        try:
            current = self.read_access_token()
            if current != rejected_token:
                self.refresh_event = "Claude CLI refresh attempted; access token changed"
            return current if current != rejected_token else None
        except RuntimeError:
            return None

    def fetch_usage(self) -> dict:
        self.refresh_event = None
        token = self.read_access_token()
        try:
            return self._fetch_with_token(token)
        except ClaudeAuthenticationError:
            refreshed = self.try_cli_refresh(token)
            if refreshed is None:
                raise ClaudeAuthenticationError(token, refresh_attempted=True) from None
            try:
                return self._fetch_with_token(refreshed)
            except ClaudeAuthenticationError:
                raise ClaudeAuthenticationError(refreshed, refresh_attempted=True) from None

    def _fetch_with_token(self, token: str) -> dict:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent(),
            "anthropic-beta": OAUTH_BETA,
        }
        request = urllib.request.Request(URL, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise ClaudeAuthenticationError(token) from None
            if exc.code == 429:
                raise ClaudeRateLimitError(exc.headers.get("Retry-After") if exc.headers else None) from None
            raise RuntimeError(f"Claude usage request failed (HTTP {exc.code})") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Claude usage request failed: {exc.reason}") from None
        if not isinstance(payload, dict):
            raise ValueError("Claude usage response was not a JSON object")
        return payload

    def parse_response(self, payload: dict) -> list[QuotaWindow]:
        if not isinstance(payload, dict):
            raise ValueError("Claude usage response was not a JSON object")
        windows = []
        for key, label, aliases, duration in (
            ("five_hour", "5h", ("five_hour", "fiveHour", "session"), 5 * 3600),
            ("weekly", "7D", ("seven_day", "sevenDay", "weekly"), 7 * 86400),
        ):
            value = next((payload.get(name) for name in aliases
                          if isinstance(payload.get(name), dict)), None)
            if value is None:
                continue
            used = numeric_percentage(
                first_value(value, "utilization", "used_percent", "usedPercent"),
                "Claude", label,
            )
            reset = reset_epoch(first_value(value, "resets_at", "reset_at", "resetAt"))
            if used is None and reset is None:
                continue
            windows.append(QuotaWindow(key, label, used, reset, duration))
        if not windows:
            raise ValueError("No recognizable Claude quota window in response")
        return windows
