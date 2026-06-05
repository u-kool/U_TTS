@echo off
cd /d "C:\1\333\models\download"
call "myenv\Scripts\activate.bat"
python download_xtts_model.py
pause
