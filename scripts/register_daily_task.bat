@echo off
REM Register the PHIP Analyzer weekday 11:00 job in Windows Task Scheduler.
SETLOCAL ENABLEEXTENSIONS

SET "REGISTER_PS1=%~dp0register_daily_task.ps1"
IF NOT EXIST "%REGISTER_PS1%" (
    echo [ERROR] register_daily_task.ps1 not found: "%REGISTER_PS1%"
    exit /b 1
)

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%REGISTER_PS1%"
EXIT /B %ERRORLEVEL%
