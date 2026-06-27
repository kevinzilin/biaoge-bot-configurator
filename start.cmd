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
"%PYEXE%" -c "import sys" >nul 2>&1
if not "%ERRORLEVEL%"=="0" (
    echo.
    echo Virtual env [.venv] exists but cannot run on this computer.
    echo Windows virtual environments are not portable between computers.
    echo Install Python 3.10+ on this computer, then run install.cmd to recreate [.venv].
    pause
    exit /b 103
)
"%PYEXE%" "scripts\launch.py" --interactive
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
    echo.
    echo Startup failed with exit code %ERR%.
)
pause
exit /b %ERR%
