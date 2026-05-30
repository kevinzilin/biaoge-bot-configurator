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
    echo Python not found.
    echo Please install Python 3.10+ [recommended: 3.12.x Windows x64] and enable "Add python.exe to PATH".
    echo Download: https://www.python.org/downloads/windows/
    exit /b 1
)

REM Detect via temp file to avoid for /f nested-quote inconsistencies across Windows builds
set "PYVER="
"%PYEXE%" -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" > "%TEMP%\_bg_pyver.tmp" 2>nul
if exist "%TEMP%\_bg_pyver.tmp" (
    set /p PYVER=<"%TEMP%\_bg_pyver.tmp"
    del "%TEMP%\_bg_pyver.tmp" 2>nul
)

if not defined PYVER (
    echo.
    echo Failed to detect Python version via "%PYEXE%".
    exit /b 1
)

for /f "tokens=1-3 delims=." %%a in ("%PYVER%") do (
    set "PYMAJ=%%a"
    set "PYMIN=%%b"
    set "PYPAT=%%c"
)

if not "%PYMAJ%"=="3" (
    echo.
    echo Python version %PYVER% is not supported.
    echo Please install Python 3.10+ [recommended: 3.12.x Windows x64].
    exit /b 1
)

REM Remove leading zeros to prevent octal interpretation in set /a
set "_PYMIN_RAW=%PYMIN%"
:strip_zero
if "%_PYMIN_RAW:~0,1%"=="0" (
    set "_PYMIN_RAW=%_PYMIN_RAW:~1%"
    goto strip_zero
)
if not defined _PYMIN_RAW set "_PYMIN_RAW=0"

set /a PYMINNUM=%_PYMIN_RAW% 2>nul
if not defined PYMINNUM set "PYMINNUM=0"

if %PYMINNUM% LSS 10 (
    echo.
    echo Python version %PYVER% is not supported.
    echo Please install Python 3.10+ [recommended: 3.12.x Windows x64].
    exit /b 1
)

echo Python OK: %PYVER% (%PYEXE%)

if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul 2>&1
        echo .env created from .env.example.
    ) else (
        type nul > ".env" 2>nul
        echo .env created [empty].
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual env [.venv] ...
    "%PYEXE%" -m venv ".venv"
    if errorlevel 1 exit /b 1
)

echo Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 exit /b 1

echo.
echo Done. Next: run start.cmd to launch.
pause
