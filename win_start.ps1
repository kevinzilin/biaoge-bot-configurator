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

& $venvPy -c "import sys" *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "Virtual env [.venv] exists but cannot run on this computer." -ForegroundColor Yellow
  Write-Host "Windows virtual environments are not portable between computers." -ForegroundColor Yellow
  Write-Host "Install Python 3.10+ on this computer, then run .\win_install.ps1 to recreate [.venv]." -ForegroundColor Yellow
  exit 103
}
& $venvPy (Join-Path $root "scripts\launch.py") --interactive
