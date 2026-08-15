@echo off
REM ============================================
REM traffic-logger install script (v1.2, P1)
REM Registers a Windows scheduled task (run at logon, background).
REM Uses project-relative paths (%~dp0); no dev-machine absolute paths.
REM Non-interactive by default (no pause); run:  install.bat interactive
REM Task name overridable via TL_TASK_NAME (tests / multi-instance).
REM ASCII-only text so the file parses regardless of console codepage.
REM ============================================
setlocal

if /I "%~1"=="interactive" (
    set "TL_INTERACTIVE=1"
) else (
    set "TL_INTERACTIVE="
)

if "%TL_TASK_NAME%"=="" set "TL_TASK_NAME=traffic_logger_autostart"
set "TASK_NAME=%TL_TASK_NAME%"

set "PROJECT_DIR=%~dp0"
set "SCRIPT=%PROJECT_DIR%traffic_logger.py"

echo [1/5] Check project files...
if not exist "%SCRIPT%" (
    echo [ERROR] Script not found: %SCRIPT%
    exit /b 1
)

echo [2/5] Check Python interpreter...
REM Probe order: env var PYTHONW, then pythonw.exe, then python.exe; else fail.
if "%PYTHONW%"=="" (
    set "PYTHONW="
    for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYTHONW set "PYTHONW=%%i"
)
if "%PYTHONW%"=="" (
    for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYTHONW set "PYTHONW=%%i"
)
if "%PYTHONW%"=="" set "PYTHONW=pythonw"
"%PYTHONW%" -c "import sys; sys.exit(0)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python interpreter unavailable: %PYTHONW%
    echo [SUGGESTION] Install Python 3.10+ and add it to PATH, or set PYTHONW.
    exit /b 1
)

echo [3/5] Remove old task (if any)...
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1

echo [4/5] Register scheduled task...
schtasks /Create /TN "%TASK_NAME%" /TR "\"%PYTHONW%\" \"%SCRIPT%\"" /SC ONLOGON /RL LIMITED /F
if errorlevel 1 (
    echo [ERROR] Task registration failed; run with admin rights if needed.
    exit /b 1
)

echo [5/5] Read back task (name / command / trigger)...
schtasks /Query /TN "%TASK_NAME%" /FO LIST /V
if errorlevel 1 (
    echo [ERROR] Task readback failed; check that the task was created.
    exit /b 1
)

echo.
echo [OK] Registered autostart task: %TASK_NAME%
echo  - Trigger: at logon (background)
echo  - Script: %SCRIPT%
echo  - Logs:   %PROJECT_DIR%logs\
echo.
echo To run in foreground:  cd /d "%PROJECT_DIR%" ^&^& python traffic_logger.py
echo Set a real api.secret in config.json before use (loopback url only).
if defined TL_INTERACTIVE (
    echo.
    pause
)
exit /b 0
