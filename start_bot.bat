@echo off
chcp 65001 >nul
title PDF Receipt Bot

cd /d "%~dp0"

echo ============================================
echo  PDF Receipt Bot — T-Bank
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python не найден. Установите Python 3.11+ с python.org
    echo.
    pause
    exit /b 1
)

echo [1/2] Проверка зависимостей...
python -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo [ERROR] Не удалось установить зависимости.
    pause
    exit /b 1
)

echo [2/2] Запуск бота...
echo.
python bot.py

echo.
echo Бот остановлен.
pause
