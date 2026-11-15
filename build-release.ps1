param([string]$Python = '')

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$payload = Join-Path $root 'release\payload'
$build = Join-Path $root '.build-release'
$dist = Join-Path $build 'dist'
$work = Join-Path $build 'work'
$spec = Join-Path $build 'spec'

if (-not $Python) {
    $repoPython = Join-Path $root '.venv\Scripts\python.exe'
    $Python = if (Test-Path -LiteralPath $repoPython) { $repoPython } else { 'py' }
}
$pythonArgs = if ($Python -eq 'py') { @('-3') } else { @() }
try {
    & $Python @pythonArgs -m PyInstaller --version | Out-Null
} catch {
    throw 'PyInstaller is unavailable. Install requirements-build.txt or pass -Python with a Python that has PyInstaller installed.'
}
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller is unavailable. Install requirements-build.txt or pass -Python with a Python that has PyInstaller installed.'
}

New-Item -ItemType Directory -Force -Path $payload,$work,$spec | Out-Null
$distFull = [IO.Path]::GetFullPath($dist)
$expectedPrefix = [IO.Path]::GetFullPath($build).TrimEnd('\') + '\'
if (-not $distFull.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to clean an unexpected build output path.'
}
if (Test-Path -LiteralPath $dist) { Remove-Item -LiteralPath $dist -Recurse -Force }
New-Item -ItemType Directory -Force -Path $dist | Out-Null

function Build-App([string]$Name, [string]$Icon, [string]$Mode, [switch]$OneDir) {
    $pyinstallerArgs = @('-m','PyInstaller','--noconfirm','--clean','--name',$Name,
        '--icon',(Join-Path $root "assets\$Icon"), '--distpath',$dist,
        '--workpath',(Join-Path $work ($Name -replace '[^A-Za-z0-9]','_')),
        '--specpath',$spec,'--paths',$root)
    if ($OneDir) { $pyinstallerArgs += @('--onedir','--contents-directory','quota-logger-internal') }
    else { $pyinstallerArgs += '--onefile' }
    if ($Mode -eq 'console') { $pyinstallerArgs += '--console' } else { $pyinstallerArgs += '--noconsole' }
    $pyinstallerArgs += (Join-Path $root 'quota_logger_app.py')
    & $Python @pythonArgs @pyinstallerArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for $Name." }
}

Build-App 'Quota Logger Config' 'quota-config.ico' 'console'
Build-App 'Quota Logger History' 'quota-history.ico' 'console'
Build-App 'quota-logger' 'quota-logger.ico' 'windowless' -OneDir

foreach ($name in @('Quota Logger Config.exe','Quota Logger History.exe')) {
    $built = Join-Path $dist $name
    if (-not (Test-Path -LiteralPath $built)) { throw "Expected build output missing: $built" }
    Copy-Item -LiteralPath $built -Destination $payload -Force
}
$collectorDir = Join-Path $dist 'quota-logger'
$collectorExe = Join-Path $collectorDir 'quota-logger.exe'
$runtimeDir = Join-Path $collectorDir 'quota-logger-internal'
if (-not (Test-Path -LiteralPath $collectorExe) -or -not (Test-Path -LiteralPath $runtimeDir)) {
    throw 'The collector executable or its runtime directory is missing.'
}
Copy-Item -LiteralPath $collectorExe -Destination $payload -Force
$payloadRuntime = Join-Path $payload 'quota-logger-internal'
if (Test-Path -LiteralPath $payloadRuntime) {
    Remove-Item -LiteralPath $payloadRuntime -Recurse -Force
}
Copy-Item -LiteralPath $runtimeDir -Destination $payloadRuntime -Recurse -Force
Write-Host "Release payload built in $payload"
