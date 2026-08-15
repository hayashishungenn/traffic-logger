@echo off
REM ============================================
REM traffic-logger 卸载脚本
REM 只停止本项目实例 + 删除本项目计划任务
REM 默认保留日志文件, 不删除任何数据
REM ============================================
setlocal

set "TASK_NAME=traffic_logger_autostart"
set "PROJECT_DIR=%~dp0"

echo [1/3] 停止本项目运行实例...
REM 仅终止命令行中包含本项目脚本路径的进程, 不波及其他程序
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -like '*%PROJECT_DIR%traffic_logger.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo [完成] 已停止本项目实例

echo [2/3] 删除计划任务...
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
echo [完成] 已删除任务 %TASK_NAME%

echo [3/3] 日志保留...
echo 日志目录: %PROJECT_DIR%logs\
echo [卸载完成] 日志数据已保留, 如需删除请手动清理

pause
