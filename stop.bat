@echo off
chcp 65001 >nul 2>&1
echo 正在停止面板、保活与系统托盘...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8877" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1
if exist "%~dp0keepalive.pid" (
    for /f "usebackq delims=" %%x in ("%~dp0keepalive.pid") do taskkill /PID %%x /F >nul 2>&1
    del /f /q "%~dp0keepalive.pid" >nul 2>&1
)
if exist "%~dp0keepalive.lock" (
    del /f /q "%~dp0keepalive.lock" >nul 2>&1
)
if exist "%~dp0tray.pid" (
    for /f "usebackq delims=" %%x in ("%~dp0tray.pid") do taskkill /PID %%x /F >nul 2>&1
    del /f /q "%~dp0tray.pid" >nul 2>&1
)
if exist "%~dp0tray.lock" (
    del /f /q "%~dp0tray.lock" >nul 2>&1
)
echo 完成。
if not defined NO_PAUSE pause
