param(
  [string]$TaskName = "BiaogeBot",
  [switch]$StopRunning
)

$ErrorActionPreference = "Stop"

schtasks /Query /TN $TaskName *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Scheduled task not found: $TaskName"
  exit 0
}

if ($StopRunning) {
  schtasks /End /TN $TaskName *> $null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Task is not running or could not be stopped: $TaskName"
  }
}

schtasks /Delete /TN $TaskName /F | Out-Host
if ($LASTEXITCODE -ne 0) {
  throw "Failed to delete scheduled task: $TaskName"
}

Write-Host "Disabled scheduled task: $TaskName"
