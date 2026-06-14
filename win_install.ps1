$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
  $py = Get-Command py -ErrorAction SilentlyContinue
}

if (-not $py) {
  Write-Host ""
  Write-Host "Python not found. Please install Python 3.10+ and enable 'Add python.exe to PATH'." -ForegroundColor Yellow
  exit 1
}

$pyExe = $py.Path
if (-not $pyExe) { $pyExe = $py.Source }

& $pyExe (Join-Path $root "scripts\bootstrap.py")
