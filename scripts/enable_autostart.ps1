param(
  [ValidateSet("Logon", "Startup")]
  [string]$Mode = "Logon",
  [string]$TaskName = "BiaogeBot",
  [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$launch = Join-Path $root "scripts\launch.py"

if (-not (Test-Path $python)) {
  throw "Virtualenv python not found: $python"
}
if (-not (Test-Path $launch)) {
  throw "Launcher not found: $launch"
}

$tr = '"' + $python + '" "' + $launch + '" --non-interactive'
if ($Mode -eq "Startup") {
  schtasks /Create /TN $TaskName /TR $tr /SC ONSTART /RU SYSTEM /RL HIGHEST /DELAY 0001:00 /F | Out-Host
} else {
  schtasks /Create /TN $TaskName /TR $tr /SC ONLOGON /RL HIGHEST /DELAY 0001:00 /F | Out-Host
}

Write-Host "Enabled scheduled task: $TaskName ($Mode)"
if ($RunNow) {
  schtasks /Run /TN $TaskName | Out-Host
} else {
  Write-Host "Start now: schtasks /Run /TN $TaskName"
}
Write-Host "Remove: schtasks /Delete /TN $TaskName /F"
