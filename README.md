# Quota Logger

Quota Logger is the planned shared home for Codex, Claude, and future quota collectors. Provider authentication and response parsing stay in provider adapters; samples and quota windows use a common SQLite schema.

## Current phase

The shared database and importer are in place, as are Codex and Claude provider adapters and a unified collector loop. The adapters retain provider-specific authentication and parsing while returning common quota-window values. The collector records each provider independently, so a provider failure is logged without preventing the next poll. This phase does not install scheduled tasks, make live API calls, or change either existing application.

The database has:

- `providers`: stable provider identifiers and display names.
- `samples`: one poll result per provider, including UTC time, success/error, raw response, and optional import provenance.
- `quota_windows`: zero or more named quota windows per successful sample, with utilization percentage, reset epoch, and optional period length.

Schema changes are tracked with SQLite `PRAGMA user_version`. The importer reads source databases in read-only transactions and is idempotent for the same source key and source sample IDs.

## Import existing databases

Run from this folder in PowerShell:

```powershell
py -3 -m quota_logger.migrate `
  --output "$env:LOCALAPPDATA\Quota Logger\quota-history.sqlite3"
```

By default, the importer reads:

- `%LOCALAPPDATA%\Codex Quota Logger\quota-history.sqlite3`
- `%LOCALAPPDATA%\Claude Quota Logger\quota-history.sqlite3`

The output path is required, so importing cannot silently write into the live application database. Override a source with `--codex-db` or `--claude-db` when needed. The importer copies samples and raw response JSON; it never reads or copies authentication credentials.

The existing databases and scheduled tasks are left untouched. For a later cutover, take a final consistent import after stopping the old collectors briefly, then start the unified collector and keep the original databases for rollback.

## Collector adapters

The Codex adapter reads `~/.codex/auth.json`; the Claude adapter reads `~/.claude/.credentials.json` or the directory selected by `CLAUDE_CONFIG_DIR`. Neither credential file is copied into the project or database. API response bodies are retained as `raw_json`, matching the existing loggers; access tokens and authorization headers are never stored.

The collector polls providers independently once per minute by default. A failed request is recorded as a failed sample for that provider and does not prevent the next adapter from running. `--once` makes one live request to each provider, so use it only when you intend to contact both quota APIs.

```powershell
py -3 -m quota_logger.collector --help
```

## Source console apps

From this folder, run History with `py -3 -m quota_logger.history --follow`. It prints one row per sample, sorted by time, with provider and dynamic quota-window columns. Failed polls are hidden by default; add `--errors` to include them. Use `-N 100` to set the initial history length.

Run Config with `py -3 -m quota_logger.config`. It shows the latest sample for each provider and offers per-user task controls. Choosing install/update, start, stop, restart, or uninstall changes the unified task; this source phase does not run those actions. `quota_history.ps1` and `quota_config.ps1` are convenience launchers for Windows PowerShell.
