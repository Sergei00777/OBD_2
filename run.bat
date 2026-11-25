@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set PY=.venv\Scripts\python.exe) else (set PY=python)
echo Проверяю библиотеки...
%PY% -m pip install -q -r requirements.txt
echo Запускаю сканер...
%PY% app.py
pause
