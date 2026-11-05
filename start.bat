@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title DeFi Alpha Scanner

rem --- 1. Поиск Python 3.10+
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if errorlevel 1 goto :oldpython

rem --- 2. Локальное окружение (создаётся один раз в папке .venv)
if exist ".venv\Scripts\python.exe" goto :deps
echo Первый запуск: создаю локальное окружение Python...
%PY% -m venv .venv
if errorlevel 1 goto :venvfail

:deps
set "VPY=.venv\Scripts\python.exe"
rem --- 3. Зависимости: ставятся при первом запуске и при изменении requirements.txt
"%VPY%" -c "import hashlib,pathlib,sys; h=hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest(); p=pathlib.Path('.venv/req.hash'); sys.exit(0 if p.exists() and p.read_text()==h else 1)"
if not errorlevel 1 goto :run
echo Установка зависимостей (1-3 минуты, нужен интернет)...
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :pipfail
"%VPY%" -c "import hashlib,pathlib; pathlib.Path('.venv/req.hash').write_text(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())"

:run
echo.
echo Запуск DeFi Alpha Scanner... Браузер откроется автоматически.
echo Если не открылся - перейдите по адресу http://127.0.0.1:8765/
"%VPY%" -m app %*
echo.
echo Программа остановлена.
pause
exit /b 0

:nopython
echo [ОШИБКА] Не найден Python.
echo Установите Python 3.11 или новее: https://www.python.org/downloads/
echo При установке отметьте галочку "Add python.exe to PATH".
echo Либо выполните в PowerShell: winget install -e --id Python.Python.3.12
pause
exit /b 1

:oldpython
echo [ОШИБКА] Установлен слишком старый Python. Нужна версия 3.11 или новее.
echo Скачать: https://www.python.org/downloads/
pause
exit /b 1

:venvfail
echo [ОШИБКА] Не удалось создать окружение .venv. Попробуйте удалить папку .venv и запустить снова.
pause
exit /b 1

:pipfail
echo [ОШИБКА] Не удалось установить зависимости. Проверьте подключение к интернету и запустите снова.
echo Подробности - в сообщениях выше. Для диагностики запустите diagnose.bat
pause
exit /b 1
