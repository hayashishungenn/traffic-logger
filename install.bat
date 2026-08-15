@echo off
REM ============================================
REM traffic-logger 安装脚本 (v1.2, T-07)
REM 注册 Windows 计划任务 (登录时后台运行)
REM 使用项目相对路径 (%%~dp0), 不依赖开发机绝对路径
REM 默认适合自动化执行 (无 pause); 交互式使用: install.bat interactive
REM 任务名可用环境变量 TL_TASK_NAME 覆盖 (测试/多实例用, 不影响其他任务)
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

echo [1/5] 检查项目文件...
if not exist "%SCRIPT%" (
    echo [错误] 找不到脚本: %SCRIPT%
    exit /b 1
)

echo [2/5] 检查 Python 解释器...
REM 优先 pythonw.exe (无控制台窗口), 回退 python.exe; 均不可用则非零退出
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
    echo [错误] Python 解释器不可用: %PYTHONW%
    echo [建议] 安装 Python 3.10+ 并加入 PATH, 或设置 PYTHONW 为解释器完整路径
    exit /b 1
)

echo [3/5] 删除旧任务(如有)...
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1

echo [4/5] 注册计划任务...
schtasks /Create /TN "%TASK_NAME%" /TR "\"%PYTHONW%\" \"%SCRIPT%\"" /SC ONLOGON /RL LIMITED /F
if errorlevel 1 (
    echo [失败] 注册任务失败, 请以管理员权限重新运行
    exit /b 1
)

echo [5/5] 回读任务配置 (任务名/命令/触发条件)...
schtasks /Query /TN "%TASK_NAME%" /FO LIST /V
if errorlevel 1 (
    echo [失败] 任务回读失败, 请检查任务是否创建成功
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
if defined TL_INTERACTIVE (
    echo.
    pause
)
exit /b 0
