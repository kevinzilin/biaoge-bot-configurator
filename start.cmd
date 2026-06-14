@echo off
setlocal
cd /d "%~dp0"

set "PYEXE=.venv\Scripts\python.exe"
if exist "%PYEXE%" goto :run

echo.
echo Virtual env [.venv] not found. Please run install.cmd first.
pause
exit /b 1

:run
"%PYEXE%" "scripts\launch.py" --interactive
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
    echo.
    echo Startup failed with exit code %ERR%.
)
pause
exit /b %ERR%
