$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Test-PythonCommand {
  param(
    [string]$Command,
    [string[]]$Arguments = @()
  )

  $resolved = Get-Command $Command -ErrorAction SilentlyContinue
  if (-not $resolved) {
    return $false
  }

  & $Command @Arguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
  return $LASTEXITCODE -eq 0
}

$pyCommand = $null
$pyArguments = @()

if (Test-PythonCommand "py" @("-3")) {
  $pyCommand = "py"
  $pyArguments = @("-3")
} elseif (Test-PythonCommand "py") {
  $pyCommand = "py"
} elseif (Test-PythonCommand "python") {
  $pyCommand = "python"
}

if (-not $pyCommand) {
  Write-Host ""
  Write-Host "Python 3.10+ not found or cannot run." -ForegroundColor Yellow
  Write-Host "Please install Python 3.10+ and enable 'Add python.exe to PATH'." -ForegroundColor Yellow
  exit 1
}

& $pyCommand @pyArguments (Join-Path $root "scripts\bootstrap.py")
