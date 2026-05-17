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

echo [1/4] Проверка обновлений...
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

echo [2/4] Остановка предыдущего экземпляра бота...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*bot.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
echo       Готово.
echo.

echo [3/4] Проверка зависимостей...
python -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo [ERROR] Не удалось установить зависимости.
    pause
    exit /b 1
)

echo [4/4] Запуск бота...
echo.
python bot.py

echo.
echo Бот остановлен.
pause
