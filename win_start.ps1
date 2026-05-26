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

function Update-DotEnv {
  param(
    [Parameter(Mandatory = $true)] [string] $Path,
    [Parameter(Mandatory = $true)] [hashtable] $Updates
  )
  if (-not (Test-Path $Path)) { return }
  $lines = Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue
  if (-not $lines) { $lines = @() }

  $existing = @{}
  foreach ($line in $lines) {
    $s = ($line -as [string])
    if (-not $s) { continue }
    if ($s.TrimStart().StartsWith("#")) { continue }
    if ($s -notmatch "=") { continue }
    $k, $v = $s.Split("=", 2)
    $kk = ($k -as [string]).Trim()
    if ($kk) { $existing[$kk] = $true }
  }

  $outLines = @()
  foreach ($line in $lines) {
    $s = ($line -as [string])
    if (-not $s) { $outLines += $line; continue }
    if ($s.TrimStart().StartsWith("#")) { $outLines += $line; continue }
    if ($s -notmatch "=") { $outLines += $line; continue }
    $k, $v = $s.Split("=", 2)
    $kk = ($k -as [string]).Trim()
    if ($kk -and $Updates.ContainsKey($kk)) {
      $outLines += ($kk + "=" + ($Updates[$kk] -as [string]))
    } else {
      $outLines += $line
    }
  }

  foreach ($k in $Updates.Keys) {
    $kk = ($k -as [string]).Trim()
    if (-not $kk) { continue }
    if ($existing.ContainsKey($kk)) { continue }
    $outLines += ($kk + "=" + ($Updates[$kk] -as [string]))
  }

  Set-Content -LiteralPath $Path -Value $outLines -Encoding ASCII
}
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Write-Host ""
  Write-Host "Virtual env (.venv) not found. Please run install.cmd first." -ForegroundColor Yellow
  exit 1
}

function Resolve-SystemPythonExe {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Path) { return $cmd.Path }
  $cmd = Get-Command py -ErrorAction SilentlyContinue
  if ($cmd) {
    try {
      $p = & py -c "import sys; print(sys.executable)" 2>$null
      $p2 = ($p | Out-String).Trim()
      if ($p2) { return $p2 }
    } catch {}
  }
  return $null
}

function Fix-VenvPyvenvCfg {
  param(
    [Parameter(Mandatory = $true)] [string] $VenvRoot,
    [string] $PythonExe = ""
  )

  $cfgPath = Join-Path $VenvRoot "pyvenv.cfg"
  if (-not (Test-Path $cfgPath)) { return }

  $lines = Get-Content -LiteralPath $cfgPath -ErrorAction SilentlyContinue
  if (-not $lines) { return }

  $kv = @{}
  foreach ($line in $lines) {
    if ($line -match '^\s*([^=]+?)\s*=\s*(.*?)\s*$') {
      $k = ($Matches[1] -as [string]).Trim()
      $v = ($Matches[2] -as [string]).Trim()
      if ($k) { $kv[$k] = $v }
    }
  }

  $exeInCfg = ""
  if ($kv.ContainsKey("executable")) { $exeInCfg = ($kv["executable"] -as [string]).Trim() }
  $exeInCfg2 = ($exeInCfg -as [string]).Trim().Trim('"')

  $needFix = $false
  if ($exeInCfg2) {
    if (-not (Test-Path $exeInCfg2)) { $needFix = $true }
  }

  if (-not $needFix) { return }
  
  $pickedExe = ""
  $candidates = @()
  if ($PythonExe) { $candidates += $PythonExe }

  if ($exeInCfg2 -match '^C:\\Users\\[^\\]+\\(.+)$') {
    $rest = ($Matches[1] -as [string]).Trim()
    if ($rest -and $env:USERPROFILE) {
      $candidates += (Join-Path $env:USERPROFILE $rest)
    }
  }

  if ($env:LOCALAPPDATA) {
    foreach ($d in @("Python312", "Python311", "Python310")) {
      $candidates += (Join-Path $env:LOCALAPPDATA ("Programs\\Python\\" + $d + "\\python.exe"))
    }
  }

  $candidates += "C:\\Program Files\\Python312\\python.exe"
  $candidates += "C:\\Program Files\\Python311\\python.exe"
  $candidates += "C:\\Program Files\\Python310\\python.exe"
  $candidates += "C:\\Program Files (x86)\\Python312\\python.exe"
  $candidates += "C:\\Program Files (x86)\\Python311\\python.exe"
  $candidates += "C:\\Program Files (x86)\\Python310\\python.exe"

  foreach ($c in $candidates) {
    $cc = ($c -as [string]).Trim().Trim('"')
    if ($cc -and (Test-Path $cc)) { $pickedExe = $cc; break }
  }

  if (-not $pickedExe) { return }

  $pyHomeDir = Split-Path -Parent $pickedExe
  $ver = ""
  try {
    $ver = & $pickedExe -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
    $ver = ($ver | Out-String).Trim()
  } catch {}
  if (-not $ver) { $ver = "3" }

  $updates = @{
    home = $pyHomeDir
    executable = $pickedExe
    version = $ver
    command = ($pickedExe + " -m venv " + $VenvRoot)
  }

  $seen = @{}
  $outLines = @()
  foreach ($line in $lines) {
    if ($line -match '^\s*([^=]+?)\s*=\s*(.*?)\s*$') {
      $k = ($Matches[1] -as [string]).Trim()
      if ($k -and $updates.ContainsKey($k)) {
        $outLines += ($k + " = " + $updates[$k])
        $seen[$k] = $true
        continue
      }
    }
    $outLines += $line
  }

  foreach ($k in @("home", "include-system-site-packages", "version", "executable", "command")) {
    if ($updates.ContainsKey($k) -and (-not $seen.ContainsKey($k))) {
      $outLines += ($k + " = " + $updates[$k])
    }
  }

  Set-Content -LiteralPath $cfgPath -Value $outLines -Encoding ASCII
  Write-Host ""
  Write-Host ("Fixed .venv\\pyvenv.cfg (executable) -> " + $pickedExe) -ForegroundColor Green
}

$sysPy = Resolve-SystemPythonExe
try {
  Fix-VenvPyvenvCfg -VenvRoot (Join-Path $root ".venv") -PythonExe $sysPy
} catch {
  Write-Host ""
  Write-Host ("Fix pyvenv.cfg failed: " + ($_ | Out-String).Trim()) -ForegroundColor Yellow
}

$envMap = Read-DotEnv -Path (Join-Path $root ".env")

if ($envMap.ContainsKey("WORKFLOW_CONFIG_PATH")) {
  $raw = ($envMap["WORKFLOW_CONFIG_PATH"] -as [string]).Trim()
  $wf = $raw.Trim('"')
  if ($wf -and (-not (Test-Path $wf))) {
    $leaf = ""
    try { $leaf = Split-Path -Leaf $wf } catch { $leaf = "" }
    $cands = @()
    $configDir = Join-Path $root "config"
    if ($wf -and (-not [System.IO.Path]::IsPathRooted($wf))) { $cands += (Join-Path $root $wf) } else { $cands += $wf }
    if ($leaf) { $cands += (Join-Path $configDir $leaf) }
    $cands += (Join-Path $configDir "workflows.loca.json")
    $cands += (Join-Path $configDir "workflows.example.json")

    $picked = ""
    foreach ($c in $cands) {
      $cc = ($c -as [string]).Trim().Trim('"')
      if ($cc -and (Test-Path $cc)) { $picked = $cc; break }
    }

    if ($picked) {
      try { $picked = (Resolve-Path -LiteralPath $picked).Path } catch {}
      Update-DotEnv -Path (Join-Path $root ".env") -Updates @{ WORKFLOW_CONFIG_PATH = $picked }
      $envMap["WORKFLOW_CONFIG_PATH"] = $picked
      Write-Host ("WORKFLOW_CONFIG_PATH fixed -> " + $picked) -ForegroundColor Green
    } else {
      Update-DotEnv -Path (Join-Path $root ".env") -Updates @{ WORKFLOW_CONFIG_PATH = "" }
      $envMap["WORKFLOW_CONFIG_PATH"] = ""
      Write-Host "WORKFLOW_CONFIG_PATH not found, cleared to allow startup." -ForegroundColor Yellow
    }
  }
}

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
