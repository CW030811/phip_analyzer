$ErrorActionPreference = "Stop"

$ProjectDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$LogDir = Join-Path $ProjectDir "logs"
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$Python = "D:\12.2\conda\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "python.exe not found: $Python"
}
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogFile = Join-Path $LogDir "daily_$Timestamp.log"

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONWARNINGS = "ignore::DeprecationWarning"
Push-Location $ProjectDir
try {
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Python main.py daily --max-items 1 --since-days 30 *> $LogFile
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldErrorActionPreference
    exit $exitCode
}
finally {
    Pop-Location
}
