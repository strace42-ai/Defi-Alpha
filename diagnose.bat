@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "VPY=.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo Окружение ещё не создано. Сначала запустите start.bat
  pause
  exit /b 1
)
"%VPY%" -m app.diagnostics
pause
