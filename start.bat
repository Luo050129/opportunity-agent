@echo off
rem Keep this file ASCII-only: cmd.exe re-reads a .bat by byte offset after a
rem codepage switch, so non-ASCII lines can be truncated into garbage commands.
setlocal
title Opportunity Agent Launcher
cd /d "%~dp0"

set "PYTHON_CMD=python"
%PYTHON_CMD% -c "import sys" >nul 2>nul
if errorlevel 1 set "PYTHON_CMD=py -3"
%PYTHON_CMD% -c "import sys" >nul 2>nul
if errorlevel 1 goto no_python

chcp 65001 >nul 2>nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto old_python

echo ============================================================
echo  Opportunity Entry ^& Analysis Agent  -  starting
echo  Folder: %CD%
echo  Entry : http://127.0.0.1:8000/
echo  Stop  : press Ctrl+C in this window
echo ============================================================
echo.

%PYTHON_CMD% "%~dp0app.py" --port 8000 --open
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" goto run_failed
exit /b 0

:no_python
echo.
echo [ERROR] Python was not found in PATH.
echo   1. Install Python 3.10 or newer: https://www.python.org/downloads/
echo   2. Tick "Add python.exe to PATH" during setup, then reopen this file.
echo   Note: the Microsoft Store alias for python does not work here.
echo.
pause
exit /b 1

:old_python
echo.
echo [ERROR] Python 3.10 or newer is required.
echo   Current command: %PYTHON_CMD%
echo   Please install a newer Python and make sure it is first in PATH.
echo.
pause
exit /b 1

:run_failed
echo.
echo [ERROR] The agent stopped with exit code %EXITCODE%.
echo   If the port is already in use, try:
echo       %PYTHON_CMD% "%~dp0app.py" --port 8801 --open
echo   To run without opening a browser, drop the --open flag.
echo.
pause
exit /b %EXITCODE%
