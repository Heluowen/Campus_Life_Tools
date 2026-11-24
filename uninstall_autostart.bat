@echo off
chcp 65001 >nul 2>&1
setlocal

echo ============================================
echo   校园生活监控 - 卸载 Windows 登录自启
echo ============================================
echo.

set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS_FILE=%STARTUP_DIR%\CampusLifeMonitor_AutoStart.vbs"

if exist "%VBS_FILE%" (
    del /f /q "%VBS_FILE%" >nul 2>&1
    echo [OK] 已移除启动项: "%VBS_FILE%"
) else (
    echo [SKIP] 未找到启动项: "%VBS_FILE%"
)

REM Also clean up any legacy scheduled tasks if present
schtasks /Delete /TN "CampusLifeMonitor_Panel" /F >nul 2>&1
schtasks /Delete /TN "CampusLifeMonitor_KeepAlive" /F >nul 2>&1

echo.
echo 完成（不会停止当前已运行的服务；如需立即停止请双击 stop.bat）。
if not defined NO_PAUSE pause
