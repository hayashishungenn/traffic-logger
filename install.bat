@echo off
REM ============================================
REM traffic-logger 安装脚本
REM 注册 Windows 计划任务 (登录时后台运行)
REM 使用项目相对路径 (%%~dp0), 不依赖开发机绝对路径
REM ============================================
setlocal

set "TASK_NAME=traffic_logger_autostart"
set "PROJECT_DIR=%~dp0"
set "SCRIPT=%PROJECT_DIR%traffic_logger.py"

echo [1/4] 检查项目文件...
if not exist "%SCRIPT%" (
    echo [错误] 找不到脚本: %SCRIPT%
    exit /b 1
)

echo [2/4] 检查 Python 解释器...
REM 优先级: 环境变量 PYTHONW > PATH 中的 pythonw > 默认 pythonw
if "%PYTHONW%"=="" (
    set "PYTHONW="
    for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYTHONW set "PYTHONW=%%i"
    if not defined PYTHONW set "PYTHONW=pythonw"
)
"%PYTHONW%" -c "import sys; sys.exit(0)" >nul 2>&1
if errorlevel 1 (
    echo [错误] Python 不可用: %PYTHONW%
    echo [建议] 安装 Python 3.10+ 并加入 PATH, 或先执行:
    echo         set PYTHONW=pythonw 的完整路径
    exit /b 1
)

echo [3/4] 删除旧任务(如有)...
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1

echo [4/4] 注册计划任务...
schtasks /Create /TN "%TASK_NAME%" /TR "\"%PYTHONW%\" \"%SCRIPT%\"" /SC ONLOGON /RL LIMITED /F
if errorlevel 1 (
    echo [失败] 注册任务失败, 请以管理员权限重新运行
    exit /b 1
)

echo.
echo [成功] 已注册开机自启任务: %TASK_NAME%
echo  - 触发: 登录时自动后台运行
echo  - 脚本: %SCRIPT%
echo  - 日志: %PROJECT_DIR%logs\
echo.
echo 如需立即前台运行:  cd /d "%PROJECT_DIR%" ^&^& python traffic_logger.py
echo 注意: 请先编辑 config.json 设置 api.secret 与 api.url (仅本机回环地址)。
pause
exit /b 0
