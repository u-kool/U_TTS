@echo off
cd /d "%~dp0"

set "PYTHONPATH=%~dp0..\.."

if exist "venv_xtts\Scripts\activate.bat" (
    call venv_xtts\Scripts\activate.bat
) else (
    call ..\..\venv_xtts\Scripts\activate.bat
)

python xtts_worker.py
pause
