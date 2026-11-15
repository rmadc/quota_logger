import contextlib
import datetime as dt
import io
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from quota_logger import config, history, task_manager
from quota_logger.database import open_database, record_sample
from quota_logger.display import date_separator, pressure_rgb
from quota_logger.models import QuotaWindow


FIVE_RESET = int(dt.datetime(2026, 11, 14, 15, tzinfo=dt.timezone.utc).timestamp())
WEEKLY_RESET = int(dt.datetime(2026, 11, 21, 10, tzinfo=dt.timezone.utc).timestamp())
DAILY_RESET = int(dt.datetime(2026, 11, 15, 10, tzinfo=dt.timezone.utc).timestamp())


class HistoryUiTests(unittest.TestCase):
    def test_history_uses_one_chronological_row_per_provider_sample(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "history.sqlite3"
            conn = open_database(path)
            try:
                record_sample(conn, "codex", "Codex", "2026-11-14T10:00:00Z",
                              windows=[
                                  QuotaWindow("five_hour", "5h", 12, FIVE_RESET, 18_000),
                                  QuotaWindow("weekly", "7D", 42, WEEKLY_RESET, 604_800),
                              ])
                record_sample(conn, "claude", "Claude", "2026-11-14T10:00:05Z",
                              windows=[
                                  QuotaWindow("five_hour", "5h", 20, FIVE_RESET, 18_000),
                                  QuotaWindow("weekly", "7D", 55, WEEKLY_RESET, 604_800),
                              ])
                record_sample(conn, "future_llm", "Future LLM", "2026-11-14T10:00:10Z",
                              windows=[QuotaWindow("daily", "24h", 8, DAILY_RESET, 86_400)])

                samples = history.fetch_samples(conn, limit=3)
                definitions = history.window_definitions(conn)
                _columns, header, width = history.build_layout(samples, definitions)
                lines = [history.format_sample(conn, sample, definitions, _columns, False)
                         for sample in samples]

                self.assertEqual([sample.provider_key for sample in samples],
                                 ["codex", "claude", "future_llm"])
                self.assertEqual({item.key for item in definitions},
                                 {"five_hour", "weekly", "daily"})
                self.assertIn("Provider", header)
                self.assertIn("24h", header)
                self.assertTrue(all(len(line) == width for line in lines))
                self.assertEqual(len(lines), 3)
            finally:
                conn.close()

    def test_errors_are_hidden_by_default_and_can_be_included(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = open_database(pathlib.Path(temp) / "history.sqlite3")
            try:
                record_sample(conn, "codex", "Codex", "2026-11-14T10:00:00Z",
                              error="fixture request failed")
                record_sample(conn, "claude", "Claude", "2026-11-14T10:01:00Z",
                              windows=[QuotaWindow("five_hour", "5h", 0, None, 18_000)])
                self.assertEqual(len(history.fetch_samples(conn, limit=10)), 1)
                visible = history.fetch_samples(conn, limit=10, include_errors=True)
                self.assertEqual(len(visible), 2)
                self.assertFalse(visible[0].ok)
                self.assertTrue(visible[1].ok)
            finally:
                conn.close()

    def test_codex_neutral_band_and_day_legend(self):
        self.assertEqual(pressure_rgb(0.9), (255, 255, 255))
        self.assertEqual(pressure_rgb(1.1), (255, 255, 255))
        legend = date_separator("2026-11-15", 48, False)
        self.assertIn("0.5x", legend)
        self.assertIn("◆ 15/11/26 ◆", legend)
        self.assertIn("1.5x", legend)


class ConfigAndTaskUiTests(unittest.TestCase):
    def test_config_status_reads_latest_sample_per_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "history.sqlite3"
            conn = open_database(path)
            try:
                record_sample(conn, "codex", "Codex", "2026-11-14T10:00:00Z",
                              windows=[QuotaWindow("five_hour", "5h", 14, None, 18_000)])
            finally:
                conn.close()
            statuses = config.provider_statuses(path)
            indexed = {row[0]: row for row in statuses}
            self.assertIsNotNone(indexed["codex"][2])
            self.assertTrue(indexed["codex"][3])
            self.assertIsNone(indexed["claude"][2])

    def test_config_dashboard_uses_mocked_task_status(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "history.sqlite3"
            conn = open_database(path)
            conn.close()
            output = io.StringIO()
            with mock.patch.object(config.task_manager, "status", return_value="Running"), \
                 contextlib.redirect_stdout(output):
                state = config.show_status(path)
            self.assertEqual(state, "Running")
            self.assertIn("Background logger task: Running", output.getvalue())
            self.assertIn("waiting for first sample", output.getvalue())

    def test_task_status_calls_are_mocked_and_read_only_by_design(self):
        mocked = SimpleNamespace(returncode=0, stdout="MISSING", stderr="")
        with mock.patch.object(task_manager.subprocess, "run", return_value=mocked) as run:
            self.assertEqual(task_manager.status(), "MISSING")
        self.assertEqual(run.call_count, 1)
        self.assertIn("Get-ScheduledTask", run.call_args.args[0][-1])

    def test_task_registration_can_be_verified_without_power_shell_execution(self):
        mocked = SimpleNamespace(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "new" / "quota.sqlite3"
            with mock.patch.object(task_manager, "_collector_command",
                                   return_value=("fake-collector.exe", "--quiet", temp)), \
                 mock.patch.object(task_manager.subprocess, "run", return_value=mocked) as run:
                task_manager.register(database)
            self.assertTrue(database.parent.is_dir())
            self.assertIn("Register-ScheduledTask", run.call_args.args[0][-1])
            self.assertEqual(run.call_args.kwargs["env"]["QUOTA_LOGGER_COLLECTOR"],
                             "fake-collector.exe")


if __name__ == "__main__":
    unittest.main()
