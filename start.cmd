@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto :run
echo.
echo Virtual env (.venv) not found. Please run install.cmd first.
pause
exit /b 1

:run
if exist "win_start.py" goto :run_py
echo.
echo Missing file: win_start.py
pause
exit /b 1

:run_py
".venv\Scripts\python.exe" "win_start.py"
pause
