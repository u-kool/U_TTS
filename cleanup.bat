@echo off
chcp 65001 >nul
title Twitch TTS - Cleanup
echo ====================================================
echo   Killing leftover Twitch TTS processes...
echo ====================================================
echo.
echo Killing leftover Python processes on ports 5000,5001, 5003, 5004, 5005, 5006, 3000...
echo.
:: Kill processes by port using netstat + taskkill
for %%p in (5000 5003 5004 5005 5006 3000) do (
    for /f "tokens=5" %%a in ('netstat -ano -p tcp ^| findstr ":%%p " ^| findstr LISTENING') do (
        echo Killing PID %%a (port %%p)
        taskkill /F /PID %%a 2>nul
    )
)

echo.
echo ====================================================
echo   Killing all python.exe processes...
echo ====================================================
taskkill /F /IM "python.exe" 2>nul
if %ERRORLEVEL% EQU 0 (
    echo Successfully killed python.exe processes.
) else (
    echo No python.exe processes found or access denied.
)

echo.
echo ====================================================
echo   Also killing orphaned processes by window title...
echo ====================================================
:: Kill orphaned worker scripts by window title or known PIDs
taskkill /F /FI "WINDOWTITLE eq Twitch*" 2>nul
taskkill /F /FI "WINDOWTITLE eq *cosyvoice*" 2>nul
taskkill /F /FI "WINDOWTITLE eq *qwen3*" 2>nul
taskkill /F /FI "WINDOWTITLE eq *xtts*" 2>nul
taskkill /F /FI "WINDOWTITLE eq *piper*" 2>nul

:: Delete stale PID file if it exists
if exist "data\server.pid" (
    echo Removing stale server.pid...
    del "data\server.pid"
)

echo.
echo Done! All leftover processes should be cleaned up.
echo You can now restart the application.