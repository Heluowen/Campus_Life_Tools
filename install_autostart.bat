@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

echo ============================================
echo   校园生活监控 - 安装 Windows 登录自启
echo ============================================
echo.

REM Auto-detect pythonw.exe: .venv -> venv -> PYTHONW_EXE -> PATH
set "PYW="
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not defined PYW if exist "%~dp0venv\Scripts\pythonw.exe" set "PYW=%~dp0venv\Scripts\pythonw.exe"
if not defined PYW if defined PYTHONW_EXE set "PYW=%PYTHONW_EXE%"
if not defined PYW (
    for /f "delims=" %%p in ('where pythonw.exe 2^>nul') do (
        if not defined PYW set "PYW=%%p"
    )
)

if not defined PYW (
    echo [ERROR] 未找到 pythonw.exe，请先创建虚拟环境 .venv 或将 Python 加入 PATH，或设置环境变量 PYTHONW_EXE。
    if not defined NO_PAUSE pause
    exit /b 1
)

set "PROJ_DIR=%~dp0"
if "%PROJ_DIR:~-1%"=="\" set "PROJ_DIR=%PROJ_DIR:~0,-1%"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS_FILE=%STARTUP_DIR%\CampusLifeMonitor_AutoStart.vbs"

echo [INFO] 使用解释器: "%PYW%"
echo [INFO] 项目目录:   "%PROJ_DIR%"
echo [INFO] 启动脚本:   "%VBS_FILE%"
echo.

REM Generate a silent VBScript launcher in the current user's Startup folder (no admin rights required, zero console flash)
> "%VBS_FILE%" (
    echo Set WshShell = CreateObject^("WScript.Shell"^)
    echo WshShell.CurrentDirectory = "%PROJ_DIR%"
    echo WshShell.Run """%PYW%"" ""%PROJ_DIR%\web\server.py"" --port 8877", 0, False
    echo WshShell.Run """%PYW%"" ""%PROJ_DIR%\keep_alive.py"" --interval 600 --log-file ""%PROJ_DIR%\keepalive.log""", 0, False
    echo WshShell.Run """%PYW%"" ""%PROJ_DIR%\tray.py"" --port 8877", 0, False
)

if not exist "%VBS_FILE%" (
    echo [ERROR] 写入启动文件夹失败，请检查权限。
    if not defined NO_PAUSE pause
    exit /b 1
)

echo [OK] 已安装当前用户登录自启（无需管理员权限，无黑窗后台运行）：
echo      开机登录后自动拉起：
echo      1. 本地 Web 面板（http://127.0.0.1:8877）
echo      2. 电费 + 燃气独立保活守护（keepalive.log）
echo      3. Windows 系统托盘图标（悬停看余额，单击开面板）
echo.
echo 如需卸载自启，请双击 uninstall_autostart.bat。
if not defined NO_PAUSE pause
