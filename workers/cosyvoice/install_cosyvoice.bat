@echo off
chcp 1251 >nul
echo Установка CosyVoice3...

:: 1. Создание venv
if not exist "venv_cosyvoice" (
    echo Создание venv_cosyvoice...
    python -m venv venv_cosyvoice
)

:: 2. Активация и установка пакетов
call .\venv_cosyvoice\Scripts\activate.bat
pip install --upgrade pip

:: Установка torch совместимой версии
pip install torch==2.3.1+cu118 torchaudio==2.3.1+cu118 --index-url https://download.pytorch.org/whl/cu118

:: Установка остальных зависимостей (numpy принудительно <2)
pip install -r venv_cosyvoice_requirements.txt

:: x-transformers and its deps --no-deps (may conflict with torch version)
pip install x-transformers einx torch-einops-utils --no-deps

:: 3. Клонирование CosyVoice репозитория (если ещё не склонирован)
if not exist "cosyvoice_repo" (
    echo Клонирование CosyVoice репозитория...
    git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git cosyvoice_repo
) else (
    echo Репозиторий уже склонирован, пропускаем...
)

:: ВАЖНО: cosyvoice не устанавливается через pip!
:: Он подключается через PYTHONPATH в start_cosyvoice_worker.bat
:: и через sys.path.insert в cosyvoice_worker.py

:: 4. Скачивание модели
echo Скачивание модели Fun-CosyVoice3-0.5B...
echo Это может занять некоторое время (несколько гигабайт)...
echo.
echo Пробуем через HuggingFace...
python -c "from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/Fun-CosyVoice3-0.5B-2512', local_dir='pretrained_models/Fun-CosyVoice3-0.5B')"
if errorlevel 1 (
    echo HuggingFace не удался, пробуем через ModelScope...
    python -c "from modelscope import snapshot_download; snapshot_download('FunAudioLLM/Fun-CosyVoice3-0.5B-2512', local_dir='pretrained_models/Fun-CosyVoice3-0.5B')"
)

echo.
echo Готово! CosyVoice3 установлен.
echo Запустите start_cosyvoice_worker.bat для запуска.
echo.
echo ЕСЛИ ОШИБКА "No module named 'cosyvoice'":
echo   Убедитесь, что папка cosyvoice_repo существует и содержит cosyvoice/
echo   Попробуйте запустить: git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git cosyvoice_repo
echo.
echo ЕСЛИ ОШИБКА "NumPy 2.x":
echo   В venv_cosyvoice установите: pip install "numpy<2"
echo.
pause
