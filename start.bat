@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

echo ============================================
echo   校园生活监控 - 一键启动（电费 + 燃气）
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
echo [INFO] 使用解释器: "%PYW%"

netstat -ano | findstr ":8877" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [SKIP] 面板服务已在运行: http://127.0.0.1:8877
) else (
    start "" "%PYW%" "%~dp0web\server.py" --port 8877
    echo [OK]   面板服务已启动（后台无窗口）: http://127.0.0.1:8877
)

REM keep_alive.py uses OS file lock for single-instance mutual exclusion
start "" "%PYW%" "%~dp0keep_alive.py" --interval 600 --log-file "%~dp0keepalive.log"
echo [OK]   保活守护已尝试拉起（单实例互斥，后台无窗口，日志: keepalive.log）

ping -n 3 127.0.0.1 >nul
start "" http://127.0.0.1:8877

echo.
echo 完成：全部后台运行、无控制台窗口。
echo 停止全部服务: 双击 stop.bat
echo 查看保活日志: type keepalive.log
if not defined NO_PAUSE pause
