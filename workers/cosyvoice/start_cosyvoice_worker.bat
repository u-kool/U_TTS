@echo off
cd /d "%~dp0"

set "PYTHONPATH=%~dp0..\..;%~dp0models\CosyVoice3\cosyvoice_repo\third_party\Matcha-TTS"

if exist "venv_cosyvoice\Scripts\activate.bat" (
    call venv_cosyvoice\Scripts\activate.bat
) else (
    call ..\..\venv_cosyvoice\Scripts\activate.bat
)

python cosyvoice_worker.py
pause
pause
