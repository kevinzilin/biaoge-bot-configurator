$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Write-Host ""
  Write-Host "Virtual env [.venv] not found. Please run .\win_install.ps1 first." -ForegroundColor Yellow
  exit 1
}

& $venvPy (Join-Path $root "scripts\launch.py") --interactive
