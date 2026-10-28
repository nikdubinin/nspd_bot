@echo off
chcp 65001 >nul
title NSPD Bot
cd /d "%~dp0"

if not exist ".env" (
    echo [!] Не найден файл .env с токеном бота.
    echo     Скопируйте config.example.env в .env и укажите TELEGRAM_BOT_TOKEN.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo Создаю виртуальное окружение...
    py -3 -m venv venv 2>nul || python -m venv venv
    if not exist "venv\Scripts\python.exe" (
        echo [!] Python не найден. Установите Python 3.12+ с python.org
        pause
        exit /b 1
    )
)

"venv\Scripts\python.exe" -c "import telegram, nspd_request, dotenv" 2>nul
if errorlevel 1 (
    echo Устанавливаю зависимости...
    "venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo [!] Не удалось установить зависимости.
        pause
        exit /b 1
    )
)

:run
echo Запуск NSPD Bot... Для остановки закройте окно или нажмите Ctrl+C.
echo.
"venv\Scripts\python.exe" bot.py
echo.
echo Бот остановлен, код завершения %errorlevel%.
choice /c YN /t 15 /d Y /m "Перезапустить? Автоматически через 15 сек"
if errorlevel 2 exit /b 0
goto run
