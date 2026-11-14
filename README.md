# Quota Logger

Quota Logger is the planned shared home for Codex, Claude, and future quota collectors. Provider authentication and response parsing stay in provider adapters; samples and quota windows use a common SQLite schema.

## Current phase

This phase defines the database and imports the existing Codex and Claude `quota_samples` databases. It does not collect live data, install scheduled tasks, or change either existing application.

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
