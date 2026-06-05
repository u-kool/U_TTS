@echo off
title Twitch TTS Tray App
echo Starting Twitch TTS Tray Application...

cd /d "%~dp0"
call myenv\Scripts\activate.bat
python tray_app.py

pause