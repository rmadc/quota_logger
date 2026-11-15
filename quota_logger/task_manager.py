"""Per-user Windows logon task controls for the unified collector."""
from __future__ import annotations

import getpass
import os
import pathlib
import subprocess
import sys
import time

from .collector import DEFAULT_DATABASE


TASK_PREFIX = "QuotaLoggerUnified"


def task_name() -> str:
    return f"{TASK_PREFIX}-{getpass.getuser()}"


def install_dir() -> pathlib.Path:
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parents[1]


def _collector_command(database: pathlib.Path = DEFAULT_DATABASE) -> tuple[str, str, str]:
    working_dir = install_dir()
    database = str(database)
    if getattr(sys, "frozen", False):
        collector = working_dir / "quota-logger.exe"
        if not collector.is_file():
            raise FileNotFoundError(f"Packaged collector not found: {collector}")
        return str(collector), f'--quiet --db "{database}"', str(working_dir)

    pythonw = pathlib.Path(sys.executable).resolve().with_name("pythonw.exe")
    if not pythonw.is_file():
        raise FileNotFoundError(f"Windowless Python was not found beside {sys.executable}: {pythonw}")
    return (str(pythonw), f'-m quota_logger.collector --quiet --db "{database}"',
            str(working_dir))


def _powershell(script: str, *, include_collector: bool = False,
                database: pathlib.Path = DEFAULT_DATABASE) -> str:
    env = os.environ.copy()
    env["QUOTA_LOGGER_TASK_NAME"] = task_name()
    if include_collector:
        executable, arguments, working_dir = _collector_command(database)
        env["QUOTA_LOGGER_COLLECTOR"] = executable
        env["QUOTA_LOGGER_ARGUMENTS"] = arguments
        env["QUOTA_LOGGER_WORKING_DIR"] = working_dir
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, env=env, check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "PowerShell returned an error.").strip()
        raise RuntimeError(detail)
    return result.stdout.strip()


def status() -> str:
    result = _powershell(r"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $env:QUOTA_LOGGER_TASK_NAME -ErrorAction SilentlyContinue
if ($null -eq $task) { [Console]::Out.Write('MISSING') }
else { [Console]::Out.Write([string]$task.State) }
""")
    return result or "Unknown"


def register(database: pathlib.Path = DEFAULT_DATABASE) -> None:
    pathlib.Path(database).parent.mkdir(parents=True, exist_ok=True)
    _powershell(r"""
$ErrorActionPreference = 'Stop'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $env:QUOTA_LOGGER_COLLECTOR `
    -Argument $env:QUOTA_LOGGER_ARGUMENTS -WorkingDirectory $env:QUOTA_LOGGER_WORKING_DIR
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $env:QUOTA_LOGGER_TASK_NAME `
    -Description 'Collect quota history for configured LLM providers.' `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
""", include_collector=True, database=database)


def start() -> None:
    _powershell("$ErrorActionPreference='Stop'; Start-ScheduledTask -TaskName $env:QUOTA_LOGGER_TASK_NAME")


def stop() -> None:
    _powershell("$ErrorActionPreference='Stop'; Stop-ScheduledTask -TaskName $env:QUOTA_LOGGER_TASK_NAME")


def restart() -> None:
    if status() == "Running":
        stop()
        time.sleep(1)
    start()


def unregister() -> None:
    if status() == "Running":
        stop()
    _powershell("$ErrorActionPreference='Stop'; Unregister-ScheduledTask -TaskName $env:QUOTA_LOGGER_TASK_NAME -Confirm:$false")
