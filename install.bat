@echo off
REM ============================================
REM traffic-logger 安装脚本 - 注册开机自启
REM 注册 Windows 计划任务: 登录时后台运行
REM ============================================
setlocal

set "TASK_NAME=traffic_logger_autostart"
set "PROJECT_DIR=C:\Users\Hayas\Github\traffic_logger"
set "PYTHONW=C:\Users\Hayas\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
set "SCRIPT=%PROJECT_DIR%\traffic_logger.py"

echo [1/3] 检查项目文件...
if not exist "%SCRIPT%" (
    echo [错误] 找不到 %SCRIPT%
    pause
    exit /b 1
)

echo [2/3] 删除旧任务(如有)...
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1

echo [3/3] 注册计划任务...
schtasks /Create /TN "%TASK_NAME%" /TR "\"%PYTHONW%\" \"%SCRIPT%\"" /SC ONLOGON /RL LIMITED /F

if %errorlevel%==0 (
    echo.
    echo [成功] 已注册开机自启任务: %TASK_NAME%
    echo  - 触发: 登录时自动后台运行
    echo  - 日志: %PROJECT_DIR%\logs\
    echo.
    echo 现在立即启动一次...
    start "" "%PYTHONW%" "%SCRIPT%"
    echo [完成] 服务已启动, 日志见 logs\ 目录
) else (
    echo [失败] 注册任务出错, 请检查权限
)

pause
