import datetime as dt
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from quota_logger.collector import collect_once
from quota_logger.database import open_database
from quota_logger.models import QuotaWindow
from quota_logger.providers.base import reset_epoch
from quota_logger.providers.claude import ClaudeAdapter
from quota_logger.providers.codex import CodexAdapter


CODEX_FIXTURE = {
    "rate_limit": {
        "primary_window": {
            "used_percent": 27,
            "reset_at": 1_800_000_000_000,
            "limit_window_seconds": 18_000,
        },
        "secondary_window": {
            "used_percent": 6,
            "reset_at": 1_800_500_000,
            "limit_window_seconds": 604_800,
        },
    }
}

CLAUDE_FIXTURE = {
    "five_hour": {"utilization": 11, "resets_at": "2026-11-14T18:00:00Z"},
    "seven_day": {"utilization": 88, "resets_at": "2026-11-18T12:00:00Z"},
}


class FakeAdapter:
    provider_key = "fake"
    display_name = "Fixture Provider"

    def fetch_usage(self):
        return {"usage": "fixture"}

    def parse_response(self, payload):
        return [QuotaWindow("session", "Session", 4.0, 1_800_000_000, 3600)]


class FailingAdapter:
    provider_key = "failure"
    display_name = "Failing Fixture"

    def fetch_usage(self):
        raise RuntimeError("fixture authentication failure")

    def parse_response(self, payload):
        raise AssertionError("Parsing should not run after fetch failure")


class ProviderAdapterTests(unittest.TestCase):
    def test_codex_fixture_maps_windows_and_millisecond_reset(self):
        windows = CodexAdapter().parse_response(CODEX_FIXTURE)
        self.assertEqual([(item.key, item.label, item.used_pct, item.window_seconds)
                          for item in windows], [
            ("five_hour", "5h", 27.0, 18_000),
            ("weekly", "7D", 6.0, 604_800),
        ])
        self.assertEqual(windows[0].reset_at_epoch, 1_800_000_000)

    def test_claude_fixture_maps_windows_and_iso_resets(self):
        windows = ClaudeAdapter(user_agent="fixture").parse_response(CLAUDE_FIXTURE)
        self.assertEqual([(item.key, item.label, item.used_pct) for item in windows], [
            ("five_hour", "5h", 11.0),
            ("weekly", "7D", 88.0),
        ])
        self.assertEqual(windows[0].reset_at_epoch, int(
            dt.datetime(2026, 11, 14, 18, tzinfo=dt.timezone.utc).timestamp()
        ))

    def test_codex_fetch_uses_mocked_response_and_local_auth_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            auth = pathlib.Path(temp) / "auth.json"
            auth.write_text(json.dumps({"tokens": {
                "access_token": "fixture-token-do-not-send",
                "account_id": "fixture-account",
            }}), encoding="utf-8")
            adapter = CodexAdapter(auth)
            response = io.BytesIO(json.dumps(CODEX_FIXTURE).encode("utf-8"))
            with mock.patch("quota_logger.providers.codex.urllib.request.urlopen",
                            return_value=response) as urlopen:
                payload = adapter.fetch_usage()
            self.assertEqual(payload, CODEX_FIXTURE)
            request = urlopen.call_args.args[0]
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers["authorization"], "Bearer fixture-token-do-not-send")
            self.assertEqual(headers["chatgpt-account-id"], "fixture-account")

    def test_claude_fetch_uses_mocked_response_and_local_auth_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            credentials = pathlib.Path(temp) / ".credentials.json"
            credentials.write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "fixture-token-do-not-send",
            }}), encoding="utf-8")
            adapter = ClaudeAdapter(credentials, user_agent="claude-code/fixture")
            response = io.BytesIO(json.dumps(CLAUDE_FIXTURE).encode("utf-8"))
            with mock.patch("quota_logger.providers.claude.urllib.request.urlopen",
                            return_value=response) as urlopen:
                payload = adapter.fetch_usage()
            self.assertEqual(payload, CLAUDE_FIXTURE)
            request = urlopen.call_args.args[0]
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers["authorization"], "Bearer fixture-token-do-not-send")
            self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")

    def test_failed_provider_is_recorded_without_blocking_next_adapter(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = pathlib.Path(temp) / "quota.sqlite3"
            conn = open_database(db_path)
            try:
                results = collect_once(conn, [FailingAdapter(), FakeAdapter()], quiet=True)
                self.assertEqual(results, {"failure": False, "fake": True})
                samples = conn.execute("""
                    SELECT p.provider_key, s.ok, s.error, s.raw_json
                    FROM samples AS s JOIN providers AS p USING(provider_id)
                    ORDER BY s.sample_id
                """).fetchall()
                self.assertEqual(samples[0][:3],
                                 ("failure", 0, "RuntimeError: fixture authentication failure"))
                self.assertIsNone(samples[0][3])
                self.assertEqual(samples[1][0:2], ("fake", 1))
                self.assertEqual(json.loads(samples[1][3]), {"usage": "fixture"})
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM quota_windows"
                ).fetchone()[0], 1)
            finally:
                conn.close()

    def test_iso_reset_parser_accepts_utc_string(self):
        expected = int(dt.datetime(2026, 11, 14, 18, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(reset_epoch("2026-11-14T18:00:00Z"), expected)


if __name__ == "__main__":
    unittest.main()
