@echo off
REM PHIP Analyzer daily scheduled job for Windows Task Scheduler.
SETLOCAL ENABLEEXTENSIONS

SET "PROJECT_DIR=%~dp0.."
SET "LOG_DIR=%PROJECT_DIR%\logs"

SET "PYTHON="
for %%P in (python.exe) do (
    if not defined PYTHON if exist "%%~$PATH:P" SET "PYTHON=%%~$PATH:P"
)
if not defined PYTHON if exist "D:\12.2\conda\python.exe" SET "PYTHON=D:\12.2\conda\python.exe"
if not defined PYTHON if exist "%USERPROFILE%\miniconda3\python.exe" SET "PYTHON=%USERPROFILE%\miniconda3\python.exe"
if not defined PYTHON if exist "%USERPROFILE%\anaconda3\python.exe" SET "PYTHON=%USERPROFILE%\anaconda3\python.exe"
if not defined PYTHON (
    echo [ERROR] python.exe not found. Please add Python to PATH.
    exit /b 1
)

IF NOT EXIST "%LOG_DIR%" MKDIR "%LOG_DIR%"
SET "TIMESTAMP=%DATE:~0,10%_%TIME:~0,2%%TIME:~3,2%"
SET "TIMESTAMP=%TIMESTAMP: =0%"
SET "LOG_FILE=%LOG_DIR%\daily_%TIMESTAMP%.log"

cd /d "%PROJECT_DIR%"
SET PYTHONIOENCODING=utf-8
"%PYTHON%" main.py daily --max-items 5 --since-days 30 >> "%LOG_FILE%" 2>&1
EXIT /B %ERRORLEVEL%
