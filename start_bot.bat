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

echo [1/3] Проверка обновлений...
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo       Git-репозиторий не найден — пропускаю обновление.
) else (
    git pull
    if errorlevel 1 (
        echo       Не удалось обновиться — запускаю текущую версию.
    ) else (
        echo       Обновление завершено.
    )
)
echo.

echo [2/3] Проверка зависимостей...
python -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo [ERROR] Не удалось установить зависимости.
    pause
    exit /b 1
)

echo [3/3] Запуск бота...
echo.
python bot.py

echo.
echo Бот остановлен.
pause
