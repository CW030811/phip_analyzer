$ErrorActionPreference = "Stop"

$ProjectDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$DailyScript = Join-Path $ProjectDir "scripts\daily.ps1"
$LauncherScript = Join-Path $env:USERPROFILE ".codex\memories\phip_daily_launcher.ps1"
$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$TaskName = "PHIP Analyzer Daily"

if (-not (Test-Path -LiteralPath $DailyScript)) {
    throw "daily.ps1 not found: $DailyScript"
}
if (-not (Test-Path -LiteralPath $LauncherScript)) {
    throw "launcher script not found: $LauncherScript"
}
if (-not (Test-Path -LiteralPath $PowerShellExe)) {
    throw "powershell.exe not found: $PowerShellExe"
}

$action = New-ScheduledTaskAction `
    -Execute $PowerShellExe `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$LauncherScript`""

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At 11:00

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Force | Out-Null

Write-Host "[OK] Registered '$TaskName' for weekdays at 11:00."
