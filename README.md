# Quota Logger

A Windows background collector and side-by-side history viewer for Codex and Claude Code quota usage. It records samples in a local SQLite database and shows 5-hour and weekly usage, reset times and pace colors.

## Requirements

- Windows with PowerShell and Task Scheduler, and Python 3.11 or newer for building from source. The installed executables do not need Python.
- Codex and/or Claude Code signed in with the relevant account. An unavailable provider is recorded as an error without stopping collection of the other provider.
- An internet connection for installing the build dependency and retrieving quota data.

The adapters use service usage endpoints and local CLI credentials. Their formats and availability can change; this project is independent of OpenAI and Anthropic.

## Build and install

Clone the repository, open PowerShell in its root, and run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build-release.ps1
.\release\install.cmd
```

If PowerShell blocks the build script, run `powershell -NoProfile -ExecutionPolicy Bypass -File .\build-release.ps1` instead. The build places its files in `release\payload`; the installer copies them to `%LOCALAPPDATA%\Programs\Quota Logger` and creates **Quota Logger Config** and **Quota Logger History** Start Menu shortcuts.

Open **Quota Logger Config**, choose the option to install the background task, and then start it. The per-user task starts the collector at logon. Open **Quota Logger History** to follow changes. Each provider's newest unresolved retrieval error appears until a successful reading replaces it. History suppresses repeated unchanged percentages.

To update, build the new release, stop the collector in Config, close both apps, and run `release\install.cmd` again. Open Config and start the collector afterwards. The installer preserves the database at `%LOCALAPPDATA%\Quota Logger\quota-history.sqlite3`.

## Run from source

From the repository root, use `py -3 -m quota_logger.history --follow` or `py -3 -m quota_logger.config`. The PowerShell launchers `quota_history.ps1` and `quota_config.ps1` provide shortcuts. Config manages the user's scheduled task; its source-mode task uses the current Python installation.

The database stores quota responses and history, which may be personal. Credential files are read locally and are not copied into the database. The service responses themselves are retained as `raw_json`.

## Database and migration

The database stores providers, per-provider samples and named quota windows. Schema changes use SQLite `PRAGMA user_version`.

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

The Codex adapter reads `~/.codex/auth.json`; the Claude adapter reads `~/.claude/.credentials.json` or the directory selected by `CLAUDE_CONFIG_DIR`. The collector polls providers independently once per minute by default. `py -3 -m quota_logger.collector --help` lists the collector options. `--once` makes a live request to both providers.

See [release/README.txt](release/README.txt) for packaging and installation details.
