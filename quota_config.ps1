param(
    [switch]$Help,
    [string]$Database
)

if ($Help) {
    @'
Quota Logger Config

Usage:
  .\quota_config.ps1 [-Database <path>] [-Help]

The Config console shows provider/task status and offers task controls.
It asks before installing/updating or uninstalling the per-user logon task.

Examples:
  .\quota_config.ps1
  .\quota_config.ps1 -Database "$env:TEMP\quota-history.sqlite3"
'@
    exit 0
}

$Host.UI.RawUI.WindowTitle = 'QUOTA LOGGER CONFIG'
$scriptArgs = @('-m', 'quota_logger.config')
if ($Database) { $scriptArgs += @('--db', $Database) }

$PSNativeCommandArgumentPassing = 'Legacy'
Push-Location $PSScriptRoot
try {
    & py -3 @scriptArgs
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
