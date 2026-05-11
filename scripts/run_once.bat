@echo off
REM PHIP Analyzer - 一次性运行（Windows）
REM 由 Windows 任务计划程序或手动调用
SETLOCAL ENABLEEXTENSIONS

SET "PROJECT_DIR=%~dp0.."
SET "LOG_DIR=%PROJECT_DIR%\logs"

REM 自动探测 Python：优先走 PATH，找不到再用常见 conda 安装路径兜底
SET "PYTHON="
for %%P in (python.exe) do (
    if not defined PYTHON if exist "%%~$PATH:P" SET "PYTHON=%%~$PATH:P"
)
if not defined PYTHON if exist "D:\12.2\conda\python.exe" SET "PYTHON=D:\12.2\conda\python.exe"
if not defined PYTHON if exist "%USERPROFILE%\miniconda3\python.exe" SET "PYTHON=%USERPROFILE%\miniconda3\python.exe"
if not defined PYTHON if exist "%USERPROFILE%\anaconda3\python.exe" SET "PYTHON=%USERPROFILE%\anaconda3\python.exe"
if not defined PYTHON (
    echo [ERROR] 未找到 python.exe，请安装 Python 或把它加入 PATH。
    exit /b 1
)

IF NOT EXIST "%LOG_DIR%" MKDIR "%LOG_DIR%"
SET "TIMESTAMP=%DATE:~0,10%_%TIME:~0,2%%TIME:~3,2%"
SET "TIMESTAMP=%TIMESTAMP: =0%"
SET "LOG_FILE=%LOG_DIR%\run_%TIMESTAMP%.log"

cd /d "%PROJECT_DIR%"
SET PYTHONIOENCODING=utf-8
"%PYTHON%" main.py run --max-items 5 >> "%LOG_FILE%" 2>&1
EXIT /B %ERRORLEVEL%
