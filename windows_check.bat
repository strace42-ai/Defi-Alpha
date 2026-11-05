@echo off
chcp 65001 >nul
cd /d "%~dp0"
title DeFi Alpha Scanner - проверка на этом компьютере
set "VPY=.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo Окружение ещё не создано. Сначала один раз запустите start.bat, затем закройте программу и запустите этот файл.
  pause
  exit /b 1
)
echo Закройте окно программы DeFi Alpha Scanner перед проверкой (проверка не трогает вашу базу, но так результат чище).
echo Установка зависимостей для тестов (один раз)...
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements-dev.txt
"%VPY%" -m app.selfcheck
echo.
echo Файл отчёта selfcheck-*.json в папке data можно передать на проверку.
pause
