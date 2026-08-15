@echo off
REM ============================================
REM traffic-logger 卸载脚本 (v1.2, T-07)
REM 只停止本项目实例 + 删除本项目计划任务
REM 默认保留日志文件, 不删除任何数据
REM 默认适合自动化执行 (无 pause); 交互式使用: uninstall.bat interactive
REM 任务名可用环境变量 TL_TASK_NAME 覆盖, 与安装脚本保持一致
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

echo [1/3] 停止本项目运行实例...
REM 只终止命令行中精确包含"规范化后的本项目脚本路径"的 python/pythonw 进程,
REM 不按模糊字符串终止无关 Python 进程; 停止后回读进程是否仍存在。
powershell -NoProfile -ExecutionPolicy Bypass -Command "$script=[IO.Path]::GetFullPath($env:TL_STOP_SCRIPT); $pred={ $_.Name -match 'python(w)?\.exe' -and $_.CommandLine -and ($_.CommandLine -replace '[\x22]','').IndexOf($script,[StringComparison]::OrdinalIgnoreCase) -ge 0 }; $p=Get-CimInstance Win32_Process | Where-Object $pred; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }; Start-Sleep -Milliseconds 300; $still=@(Get-CimInstance Win32_Process | Where-Object $pred); if ($still.Count -gt 0) { Write-Host 'ERROR_NOT_STOPPED'; exit 1 } else { Write-Host 'STOPPED_OK'; exit 0 }"
if errorlevel 1 (
    echo [错误] 仍有本项目实例未停止; 日志目录已保留
    exit /b 1
)
echo [完成] 已停止本项目实例

echo [2/3] 删除计划任务...
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
if errorlevel 1 (
    echo [错误] 任务删除失败 (可能不存在)
    exit /b 1
)
echo [完成] 已删除任务 %TASK_NAME%

echo [3/3] 日志保留...
echo 日志目录: %PROJECT_DIR%logs\
echo [卸载完成] 日志数据已保留, 如需删除请手动清理

if defined TL_INTERACTIVE (
    echo.
    pause
)
exit /b 0
