@echo off
setlocal
color 0B
set "APP=Quota Logger"
set "SOURCE=%~dp0payload"
set "PROGRAMS=%LOCALAPPDATA%\Programs\%APP%"
set "DATA=%LOCALAPPDATA%\%APP%"

echo.
echo        /\_/\
echo       ( o.o )   QUOTA LOGGER
echo        ^> ^<      INSTALLER
echo.
echo   This copies or updates the apps and leaves your history database alone.
echo.

for %%F in ("Quota Logger Config.exe" "Quota Logger History.exe" "quota-logger.exe") do (
  if not exist "%SOURCE%\%%~F" (
    color 0C
    echo Missing release file: "%SOURCE%\%%~F"
    echo Run ..\build-release.ps1 to build the release payload.
    echo.
    pause
    exit /b 1
  )
)
if not exist "%SOURCE%\quota-logger-internal" (
  color 0C
  echo Missing collector runtime folder: "%SOURCE%\quota-logger-internal"
  echo Run ..\build-release.ps1 to build the release payload.
  echo.
  pause
  exit /b 1
)

color 0E
echo   Close any running Quota Logger apps before updating them.
echo   This does not create, start, stop, or change a scheduled task.
echo.
choice /C YN /M "Copy or update the Quota Logger app files"
if errorlevel 2 (
  color 0E
  echo   No files were changed.
  echo.
  pause
  exit /b 0
)

if not exist "%PROGRAMS%" (
  mkdir "%PROGRAMS%"
  if errorlevel 1 goto :copy_failed
)
if not exist "%DATA%" (
  mkdir "%DATA%"
  if errorlevel 1 goto :copy_failed
)

color 0B
echo   [1/3] Updating Config...
copy /Y "%SOURCE%\Quota Logger Config.exe" "%PROGRAMS%\Quota Logger Config.exe" >nul
if errorlevel 1 goto :copy_failed
echo   [2/3] Updating History...
copy /Y "%SOURCE%\Quota Logger History.exe" "%PROGRAMS%\Quota Logger History.exe" >nul
if errorlevel 1 goto :copy_failed
echo   [3/3] Updating the quiet collector...
if not exist "%PROGRAMS%\quota-logger-internal" mkdir "%PROGRAMS%\quota-logger-internal"
robocopy "%SOURCE%\quota-logger-internal" "%PROGRAMS%\quota-logger-internal" /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NC /NS /NP >nul
if errorlevel 8 goto :copy_failed
copy /Y "%SOURCE%\quota-logger.exe" "%PROGRAMS%\quota-logger.exe" >nul
if errorlevel 1 goto :copy_failed

echo   Making Start Menu shortcuts...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=[Environment]::GetFolderPath('Programs'); $w=New-Object -ComObject WScript.Shell; foreach($n in @('Config','History')){$name='Quota Logger '+$n;$file=$name+'.exe';$s=$w.CreateShortcut((Join-Path $p ($name+'.lnk')));$s.TargetPath=Join-Path $env:LOCALAPPDATA ('Programs\Quota Logger\'+$file);$s.WorkingDirectory=Join-Path $env:LOCALAPPDATA 'Programs\Quota Logger';$s.IconLocation=$s.TargetPath+',0';$s.Save()}"
if errorlevel 1 (color 0E & echo   Shortcuts could not be created. Open the EXEs from "%PROGRAMS%".)

echo.
color 0A
echo   Quota Logger is installed.
color 0B
echo   Apps:   "%PROGRAMS%"
echo   Data:   "%DATA%\quota-history.sqlite3"
echo.
echo   Open Quota Logger Config when you are ready to configure the logon task.
echo   No scheduled task was changed by this installer.
if not exist "%USERPROFILE%\.codex\auth.json" echo   Note: Codex collection needs a signed-in Codex account.
if not exist "%USERPROFILE%\.claude\.credentials.json" echo   Note: Claude collection needs Claude Code credentials.
echo.
pause
exit /b 0

:copy_failed
color 0C
echo.
echo   A file could not be copied. Close Quota Logger apps and stop its task
echo   in Config before updating, then run install.cmd again.
echo.
pause
exit /b 1
