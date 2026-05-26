$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Read-DotEnv {
  param([Parameter(Mandatory = $true)] [string] $Path)
  $map = @{}
  if (-not (Test-Path $Path)) { return $map }
  $lines = Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue
  foreach ($line in $lines) {
    $s = ($line -as [string]).Trim()
    if (-not $s) { continue }
    if ($s.StartsWith("#")) { continue }
    $idx = $s.IndexOf("=")
    if ($idx -lt 1) { continue }
    $k = $s.Substring(0, $idx).Trim()
    $v = $s.Substring($idx + 1).Trim()
    if ($k) { $map[$k] = $v }
  }
  return $map
}

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Write-Host ""
  Write-Host "Virtual env (.venv) not found. Please run install.cmd first." -ForegroundColor Yellow
  exit 1
}

$envMap = Read-DotEnv -Path (Join-Path $root ".env")
$cbHost = "127.0.0.1"
$cbPort = 9901
if ($envMap.ContainsKey("CALLBACK_HOST")) {
  $h = ($envMap["CALLBACK_HOST"] -as [string]).Trim()
  if ($h) { $cbHost = $h.Trim('"') }
}
if ($envMap.ContainsKey("CALLBACK_PORT")) {
  $p = ($envMap["CALLBACK_PORT"] -as [string]).Trim().Trim('"')
  if ($p) {
    try { $cbPort = [int]$p } catch {}
  }
}

try {
  $listening = Get-NetTCPConnection -State Listen -LocalPort $cbPort -ErrorAction SilentlyContinue
  if ($listening) {
    Write-Host ""
    Write-Host ("Port already in use: " + $cbHost + ":" + $cbPort) -ForegroundColor Yellow
    Write-Host "Please close the existing process, or change CALLBACK_PORT in .env, then run start.cmd again." -ForegroundColor Yellow
    exit 1
  }
} catch {}

Write-Host "Starting biaoge_bot ..." -ForegroundColor Cyan
Write-Host ("Config page: http://" + $cbHost + ":" + $cbPort + "/admin/config?token=<ADMIN_TOKEN>") -ForegroundColor Cyan
Write-Host ""

& $venvPy -m biaoge_bot.main
