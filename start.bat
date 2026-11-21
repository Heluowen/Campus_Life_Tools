@echo off
setlocal
cd /d "%~dp0"
title MonitorLauncher

echo ============================================
echo   校园生活监控 · 一键启动（电费 + 燃气）
echo ============================================
echo.

REM 1) Web panel (port 8877; skip if already running)
netstat -ano | findstr ":8877" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [SKIP] 面板服务已在运行: http://127.0.0.1:8877
) else (
    start "电费面板" /min cmd /c "python web\server.py --port 8877"
    echo [OK]   面板服务已启动: http://127.0.0.1:8877
)

REM 2) Keep-alive daemon (single-instance lock protects against duplicates)
start "保活守护" /min cmd /c "python keep_alive.py --interval 600 --log-file keepalive.log"
echo [OK]   保活守护已启动（10 分钟/次，日志: keepalive.log）

REM 3) Open browser
ping -n 3 127.0.0.1 >nul
start "" http://127.0.0.1:8877

echo.
echo 完成。窗口可最小化，关闭本窗口不影响后台。
echo 停止全部服务: 双击 stop.bat
echo 查看保活日志: type keepalive.log
if not defined NO_PAUSE pause
