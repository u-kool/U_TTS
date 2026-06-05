@echo off
chcp 1251 >nul
echo ========================================================
echo   Установка окружения (venv + зависимости)
echo ========================================================
echo(
python -m venv myenv
call .\myenv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
echo(
echo Готово!
pause
