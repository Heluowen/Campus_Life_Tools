@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   校园生活监控 - 一键启动（电费 + 燃气）
echo ============================================
echo.

netstat -ano | findstr ":8877" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [SKIP] 面板服务已在运行: http://127.0.0.1:8877
) else (
    start "" "H:\AstrBot\backend\python\pythonw.exe" "%~dp0web\server.py" --port 8877
    echo [OK]   面板服务已启动（后台无窗口）: http://127.0.0.1:8877
)

if exist "%~dp0keepalive.lock" (
    echo [SKIP] 保活守护已在运行（keepalive.lock）
) else (
    start "" "H:\AstrBot\backend\python\pythonw.exe" "%~dp0keep_alive.py" --interval 600 --log-file "%~dp0keepalive.log"
    echo [OK]   保活守护已启动（后台无窗口，日志: keepalive.log）
)

ping -n 3 127.0.0.1 >nul
start "" http://127.0.0.1:8877

echo.
echo 完成：全部后台运行、无控制台窗口。
echo 停止全部服务: 双击 stop.bat
echo 查看保活日志: type keepalive.log
if not defined NO_PAUSE pause
