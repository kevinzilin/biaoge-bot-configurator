$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Resolve-Python {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { return @{ exe = $cmd.Path; via = "python" } }
  $cmd = Get-Command py -ErrorAction SilentlyContinue
  if ($cmd) { return @{ exe = "py"; via = "py" } }
  return $null
}

function Get-PythonVersion {
  param([Parameter(Mandatory = $true)] [string] $Exe)
  $ver = & $Exe -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
  return ($ver | Out-String).Trim()
}

function Test-PythonVersionOk {
  param([Parameter(Mandatory = $true)] [string] $Version)
  $parts = $Version.Split(".")
  if ($parts.Count -lt 2) { return $false }
  $maj = [int]$parts[0]
  $min = [int]$parts[1]
  if ($maj -ne 3) { return $false }
  return ($min -ge 10)
}

$pyInfo = Resolve-Python
if (-not $pyInfo) {
  Write-Host ""
  Write-Host "Python not found." -ForegroundColor Yellow
  Write-Host "Please install Python 3.10+ (recommended: 3.12.x Windows x64) and enable 'Add python.exe to PATH'." -ForegroundColor Yellow
  Write-Host "Download: https://www.python.org/downloads/windows/" -ForegroundColor Yellow
  try { Start-Process "https://www.python.org/downloads/windows/" | Out-Null } catch {}
  exit 1
}

$pyExe = $pyInfo.exe
$pyVer = Get-PythonVersion -Exe $pyExe
if (-not (Test-PythonVersionOk -Version $pyVer)) {
  Write-Host ""
  Write-Host "Python version $pyVer is not supported." -ForegroundColor Yellow
  Write-Host "Please install Python 3.10+ (recommended: 3.12.x Windows x64)." -ForegroundColor Yellow
  Write-Host "Download: https://www.python.org/downloads/windows/" -ForegroundColor Yellow
  try { Start-Process "https://www.python.org/downloads/windows/" | Out-Null } catch {}
  exit 1
}

Write-Host ("Python OK: " + $pyVer + " (" + $pyInfo.via + ")") -ForegroundColor Green

if (-not (Test-Path ".env")) {
  if (Test-Path ".env.example") {
    Copy-Item ".env.example" ".env" -Force
    Write-Host ".env created from .env.example." -ForegroundColor Green
  } else {
    New-Item -ItemType File -Path ".env" -Force | Out-Null
    Write-Host ".env created (empty)." -ForegroundColor Yellow
  }
}

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Write-Host "Creating virtual env (.venv) ..." -ForegroundColor Cyan
  & $pyExe -m venv ".venv"
}

Write-Host "Installing dependencies ..." -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r "requirements.txt"

Write-Host ""
Write-Host "Done. Next: run start.cmd to launch." -ForegroundColor Green
