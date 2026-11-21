@echo off
echo 正在停止电费面板与保活守护进程...
taskkill /FI "WINDOWTITLE eq 电费面板*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq 保活守护*" /T /F >nul 2>&1
if exist keepalive.lock del /f /q keepalive.lock >nul 2>&1
echo 完成。
if not defined NO_PAUSE pause
