@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE="
where python >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
if not defined PYEXE (
  where py >nul 2>nul
  if not errorlevel 1 set "PYEXE=py"
)

if not defined PYEXE (
  echo.
  echo Python not found.
  echo Please install Python 3.10+ (recommended: 3.12.x Windows x64) and enable "Add python.exe to PATH".
  echo Download: https://www.python.org/downloads/windows/
  exit /b 1
)

for /f "delims=" %%V in ('%PYEXE% -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2^>nul') do set "PYVER=%%V"
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
  echo Please install Python 3.10+ (recommended: 3.12.x Windows x64).
  exit /b 1
)

set /a PYMINNUM=%PYMIN% >nul 2>nul
if %PYMINNUM% LSS 10 (
  echo.
  echo Python version %PYVER% is not supported.
  echo Please install Python 3.10+ (recommended: 3.12.x Windows x64).
  exit /b 1
)

echo Python OK: %PYVER% (%PYEXE%)

if not exist ".env" (
  if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo .env created from .env.example.
  ) else (
    type nul > ".env"
    echo .env created (empty).
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual env (.venv) ...
  %PYEXE% -m venv ".venv"
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
