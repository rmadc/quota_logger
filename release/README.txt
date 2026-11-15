Quota Logger release
====================

Build
-----
Run build-release.ps1 from the project root. The build needs Python 3.11 or
newer and the pinned PyInstaller dependency from requirements-build.txt. To
install the build dependency into a local virtual environment:

  py -3 -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements-build.txt
  .\build-release.ps1 -Python .venv\Scripts\python.exe

The target computer does not need Python. The generated executables are placed
in release\payload.

Install or update
-----------------
Run install.cmd. It copies the Config and History apps and the quiet collector
to:

  %LOCALAPPDATA%\Programs\Quota Logger

It creates Start Menu shortcuts and keeps the shared database in:

  %LOCALAPPDATA%\Quota Logger\quota-history.sqlite3

The installer does not register or change the background task. Open Config
when you choose to configure collection. Config asks before installing or
removing the per-user logon task. The collector reads the current user's
Codex or Claude Code credentials; credentials are not included in this package.

The history database is never included in the package or overwritten by the
installer. Existing provider databases are not removed.
