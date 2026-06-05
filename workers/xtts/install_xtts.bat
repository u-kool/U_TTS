@echo off
chcp 65001 >nul
echo Установка XTTS...

if not exist "venv_xtts" (
    echo Создание venv_xtts...
    python -m venv venv_xtts
)

call .\venv_xtts\Scripts\activate.bat
pip install --upgrade pip

:: Установка torch с CUDA
pip install torch==2.5.1+cu124 torchaudio==2.5.1+cu124 --index-url https://download.pytorch.org/whl/cu124

:: Установка остальных зависимостей
pip install -r venv_xtts_requirements.txt

echo.
echo Готово! XTTS установлен.
echo Запустите start_xtts_worker.bat для запуска.
pause
