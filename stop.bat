@echo off
echo 正在停止面板与保活...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8877" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1
if exist "%~dp0keepalive.lock" (
    for /f "delims=" %%x in (%~dp0keepalive.lock) do taskkill /PID %%x /F >nul 2>&1
    del /f /q "%~dp0keepalive.lock" >nul 2>&1
)
echo 完成。
if not defined NO_PAUSE pause
