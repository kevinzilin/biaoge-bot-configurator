@echo off
setlocal
cd /d "%~dp0"

set "PYEXE="
call :try_python py -3
if not defined PYEXE call :try_python py
if not defined PYEXE call :try_python python

if not defined PYEXE (
    echo.
    echo Python 3.10+ not found or cannot run.
    echo Please install Python 3.10+ and enable "Add python.exe to PATH".
    pause
    exit /b 1
)

%PYEXE% "scripts\bootstrap.py"
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
    echo.
    echo Install failed with exit code %ERR%.
)
pause
exit /b %ERR%

:try_python
%* -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if "%ERRORLEVEL%"=="0" set "PYEXE=%*"
exit /b 0
