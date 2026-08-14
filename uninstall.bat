@echo off
REM ============================================
REM traffic-logger 卸载脚本
REM 停止运行中的进程 + 删除计划任务
REM ============================================
setlocal

set "TASK_NAME=traffic_logger_autostart"
set "PROJECT_DIR=C:\Users\Hayas\Github\traffic_logger"
set "PYTHONW=C:\Users\Hayas\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"

echo [1/3] 停止运行中的实例...
wmic process where "name='pythonw.exe' and commandline like '%%traffic_logger%%'" call terminate >nul 2>&1
wmic process where "name='python.exe' and commandline like '%%traffic_logger%%'" call terminate >nul 2>&1
echo [完成] 已停止

echo [2/3] 删除计划任务...
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
echo [完成] 已删除任务 %TASK_NAME%

echo [3/3] 保留日志文件: %PROJECT_DIR%\logs\
echo [卸载完成] 日志数据已保留, 如需删除请手动清理

pause
