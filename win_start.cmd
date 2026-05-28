@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if defined PYEXE goto :py_ok
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
  pause
  exit /b 1
)

:py_ok
"%PYEXE%" "%~dp0win_start.py"
pause
