@echo off
REM ============================================
REM traffic-logger uninstall script (v1.2, P1)
REM Stops only this project's instance + deletes this project's scheduled task.
REM Keeps logs/ by default; deletes nothing else.
REM Non-interactive by default; run:  uninstall.bat interactive
REM Task name overridable via TL_TASK_NAME (must match the install script).
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
set "TL_STOP_SCRIPT=%PROJECT_DIR%traffic_logger.py"

echo [1/3] Stop this project's running instance...
REM Stop only python/pythonw processes whose command line contains the
REM normalized project script path as a whole token (not a fuzzy substring);
REM never stop unrelated Python processes. Read back whether one still runs.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$script=[IO.Path]::GetFullPath($env:TL_STOP_SCRIPT); $pat='(?i)(?<![\w.])' + [regex]::Escape($script) + '(?![\w.])'; $pred={ $_.Name -match 'python(w)?\.exe' -and $_.CommandLine -and [regex]::IsMatch(($_.CommandLine -replace '[\x22]',''), $pat) }; $p=Get-CimInstance Win32_Process | Where-Object $pred; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }; Start-Sleep -Milliseconds 300; $still=@(Get-CimInstance Win32_Process | Where-Object $pred); if ($still.Count -gt 0) { Write-Host 'ERROR_NOT_STOPPED'; exit 1 } else { Write-Host 'STOPPED_OK'; exit 0 }"
if errorlevel 1 (
    echo [ERROR] An instance is still running; logs are preserved.
    exit /b 1
)
echo [OK] Stopped this project's instance.

echo [2/3] Delete scheduled task...
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Task deletion failed (it may not exist).
    exit /b 1
)
echo [OK] Deleted task %TASK_NAME%

echo [3/3] Logs preserved...
echo Log directory: %PROJECT_DIR%logs\
echo [OK] Uninstall complete; logs kept. Delete manually if needed.

if defined TL_INTERACTIVE (
    echo.
    pause
)
exit /b 0
