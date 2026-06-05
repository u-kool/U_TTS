@echo off
title Building TwitchTTS.exe
cd /d "%~dp0"

echo Cleaning old build...
if exist "dist\TwitchTTS.exe" del "dist\TwitchTTS.exe"

echo Running build_exe.py...
python build_exe.py

if errorlevel 1 (
    echo Build failed (errorlevel %errorlevel%)
    pause
    exit /b 1
)

echo Cleaning build artifacts...
if exist "build" rmdir /s /q "build"
if exist "TwitchTTS.spec" del "TwitchTTS.spec"

echo Done: dist\TwitchTTS.exe
pause
