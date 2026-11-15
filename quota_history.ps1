param(
    [int]$N = 100,
    [switch]$Follow,
    [switch]$Errors,
    [switch]$NoColor,
    [switch]$Help,
    [string]$Database
)

if ($Help) {
    @'
Quota Logger History

Usage:
  .\quota_history.ps1 [-N <count>] [-Follow] [-Errors] [-NoColor] [-Database <path>]

Options:
  -N <count>      Number of most recent samples shown initially (default: 100).
  -Follow         Keep appending samples. Press Ctrl+C to stop.
  -Errors         Include failed polls.
  -NoColor        Disable quota colors.
  -Database <path> Use a specific SQLite database.
  -Help           Show this help.

Examples:
  .\quota_history.ps1
  .\quota_history.ps1 -N 40 -Follow
'@
    exit 0
}

$Host.UI.RawUI.WindowTitle = 'QUOTA LOGGER HISTORY'
$scriptArgs = @('-m', 'quota_logger.history', "$N")
if ($Follow) { $scriptArgs += '--follow' }
if ($Errors) { $scriptArgs += '--errors' }
if ($NoColor) { $scriptArgs += '--no-color' }
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
