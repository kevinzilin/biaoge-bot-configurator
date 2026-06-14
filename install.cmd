@echo off
setlocal
cd /d "%~dp0"

set "PYEXE="
where python >nul 2>&1
if %errorlevel%==0 set "PYEXE=python"

if not defined PYEXE (
    where py >nul 2>&1
    if %errorlevel%==0 set "PYEXE=py"
)

if not defined PYEXE (
    echo.
    echo Python not found. Please install Python 3.10+ and enable "Add python.exe to PATH".
    pause
    exit /b 1
)

"%PYEXE%" "scripts\bootstrap.py"
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
    echo.
    echo Install failed with exit code %ERR%.
)
pause
exit /b %ERR%
