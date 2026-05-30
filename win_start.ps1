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

function New-RandomToken {
  $bytes = [byte[]]::new(16)
  [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
  return [System.Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '_').Replace('/', '-')
}

$WORKFLOWS_LOCAL_SKELETON = @'
{
  "_comment": "Minimal config skeleton. See config/workflows.example.json for full reference.",
  "default_table": "",
  "default_workflow": "",
  "tables": {},
  "automation": {},
  "workflows": {}
}
'@

function Ensure-WorkflowsLocalConfig {
  param([Parameter(Mandatory = $true)] [string] $RootDir)
  $localPath = Join-Path $RootDir "config\workflows.local.json"
  if (-not (Test-Path $localPath)) {
    $dir = Split-Path -Parent $localPath
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Set-Content -LiteralPath $localPath -Value $WORKFLOWS_LOCAL_SKELETON.Trim() -Encoding UTF8
    Write-Host "Created config\workflows.local.json (minimal skeleton)" -ForegroundColor Green
  }
}

$REQUIRED_ENV_KEYS = @(
  ,@("FEISHU_APP_ID", "Feishu App ID")
  ,@("FEISHU_APP_SECRET", "Feishu App Secret")
)

$OPTIONAL_ENV_KEYS = @(
  ,@("ADMIN_TOKEN", "Admin token (leave blank to auto-generate)")
)

function Ensure-RequiredConfig {
  param([Parameter(Mandatory = $true)] [string] $RootDir)
  $envPath = Join-Path $RootDir ".env"
  $envMap = Read-DotEnv -Path $envPath

  $missing = @()
  foreach ($entry in $REQUIRED_ENV_KEYS) {
    $key = $entry[0]; $desc = $entry[1]
    $val = ""
    if ($envMap.ContainsKey($key)) { $val = ($envMap[$key] -as [string]).Trim() }
    if (-not $val) { $missing += ,@($key, $desc, $true) }
  }
  foreach ($entry in $OPTIONAL_ENV_KEYS) {
    $key = $entry[0]; $desc = $entry[1]
    $val = ""
    if ($envMap.ContainsKey($key)) { $val = ($envMap[$key] -as [string]).Trim() }
    if (-not $val) { $missing += ,@($key, $desc, $false) }
  }

  if ($missing.Count -eq 0) { return $true }

  Write-Host ""
  Write-Host ("=" * 60)
  Write-Host "  Missing configuration. Please enter values below:"
  Write-Host "  (Ctrl+C to cancel)"
  Write-Host ("=" * 60)

  $updates = @{}
  foreach ($entry in $missing) {
    $key = $entry[0]; $desc = $entry[1]; $required = $entry[2]
    Write-Host ""
    Write-Host ("  [" + $desc + "]")
    $value = Read-Host -Prompt ("  " + $key + " =")
    $value = $value.Trim()
    if (-not $value) {
      if ($required) {
        Write-Host ""
        Write-Host ("  ! " + $key + " is required. Cancelled.") -ForegroundColor Yellow
        return $false
      }
      $value = New-RandomToken
      Write-Host ("  -> Auto-generated: " + $value) -ForegroundColor Green
    }
    $updates[$key] = $value
  }

  # Copy .env.example if .env does not exist
  if (-not (Test-Path $envPath)) {
    $examplePath = Join-Path $RootDir ".env.example"
    if (Test-Path $examplePath) {
      Copy-Item -LiteralPath $examplePath -Destination $envPath -Force
    }
  }

  Update-DotEnv -Path $envPath -Updates $updates

  foreach ($k in $updates.Keys) {
    [Environment]::SetEnvironmentVariable($k, $updates[$k], "Process")
  }

  Write-Host ""
  Write-Host "  Saved to .env" -ForegroundColor Green
  Write-Host ""
  return $true
}

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Write-Host ""
  Write-Host "Virtual env (.venv) not found. Please run win_install.ps1 first." -ForegroundColor Yellow
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

function Fix-VenvActivationScripts {
  param([Parameter(Mandatory = $true)] [string] $VenvRoot)

  $vr = $VenvRoot
  try { $vr = (Resolve-Path -LiteralPath $VenvRoot).Path } catch {}
  if (-not $vr) { return }
  $vrWin = $vr

  $actBat = Join-Path $VenvRoot "Scripts\activate.bat"
  if (Test-Path $actBat) {
    try {
      $lines = Get-Content -LiteralPath $actBat -ErrorAction SilentlyContinue
      if ($lines) {
        $changed = $false
        $out = @()
        foreach ($line in $lines) {
          if ($line -match '^set\s+VIRTUAL_ENV=') {
            $out += ("set VIRTUAL_ENV=" + $vrWin)
            $changed = $true
          } else {
            $out += $line
          }
        }
        if ($changed) { Set-Content -LiteralPath $actBat -Value $out -Encoding UTF8 }
      }
    } catch {}
  }

  $actSh = Join-Path $VenvRoot "Scripts\activate"
  if (Test-Path $actSh) {
    try {
      $txt = Get-Content -LiteralPath $actSh -Raw -ErrorAction SilentlyContinue
      if ($txt) {
        $txt2 = $txt
        $txt2 = [regex]::Replace($txt2, 'cygpath\s+"[^"]*?\\.venv"', ('cygpath "' + $vrWin.Replace('\','\\') + '"'))
        $txt2 = [regex]::Replace($txt2, 'export\s+VIRTUAL_ENV="[^"]*?\\.venv"', ('export VIRTUAL_ENV="' + $vrWin.Replace('\','\\') + '"'))
        if ($txt2 -ne $txt) { Set-Content -LiteralPath $actSh -Value $txt2 -Encoding UTF8 }
      }
    } catch {}
  }
}

try {
  Fix-VenvActivationScripts -VenvRoot (Join-Path $root ".venv")
} catch {}

function Ensure-VenvModule {
  param(
    [Parameter(Mandatory = $true)] [string] $VenvPythonExe,
    [Parameter(Mandatory = $true)] [string] $ModuleName,
    [Parameter(Mandatory = $true)] [string] $PipPackageName
  )
  try {
    & $VenvPythonExe -c ("import " + $ModuleName) 2>$null
    if ($LASTEXITCODE -eq 0) { return }
  } catch {}
  Write-Host ""
  Write-Host ("Installing missing dependency: " + $PipPackageName) -ForegroundColor Cyan
  try { & $VenvPythonExe -m pip install --upgrade pip } catch {}
  & $VenvPythonExe -m pip install $PipPackageName
}

try {
  Ensure-VenvModule -VenvPythonExe $venvPy -ModuleName "multipart" -PipPackageName "python-multipart"
} catch {
  Write-Host ""
  Write-Host ("Dependency check failed: " + ($_ | Out-String).Trim()) -ForegroundColor Yellow
}

$envMap = Read-DotEnv -Path (Join-Path $root ".env")

# --- Fix / resolve WORKFLOW_CONFIG_PATH ---
if ($envMap.ContainsKey("WORKFLOW_CONFIG_PATH")) {
  $raw = ($envMap["WORKFLOW_CONFIG_PATH"] -as [string]).Trim()
  $wf = $raw.Trim('"')
  $configDir = Join-Path $root "config"
  $envPath = Join-Path $root ".env"

  # Resolve relative path to absolute based on project root
  if ($wf -and (-not [System.IO.Path]::IsPathRooted($wf))) {
    $wf = Join-Path $root $wf
  }

  # Path exists -> write absolute path back to .env
  if ($wf -and (Test-Path $wf)) {
    try { $absPath = (Resolve-Path -LiteralPath $wf).Path } catch { $absPath = $wf }
    if ($absPath -ne $raw) {
      Update-DotEnv -Path $envPath -Updates @{ WORKFLOW_CONFIG_PATH = $absPath }
      $envMap["WORKFLOW_CONFIG_PATH"] = $absPath
      Write-Host ("WORKFLOW_CONFIG_PATH -> " + $absPath) -ForegroundColor Green
    }
  } else {
    # Path missing -> search for alternatives in config dir
    $cands = @()
    if ($wf) {
      $leaf = ""
      try { $leaf = Split-Path -Leaf $wf } catch {}
      if ($leaf) { $cands += (Join-Path $configDir $leaf) }
    }
    $cands += (Join-Path $configDir "workflows.local.json")
    $cands += (Join-Path $configDir "workflows.example.json")

    $picked = ""
    foreach ($c in $cands) {
      $cc = ($c -as [string]).Trim().Trim('"')
      if ($cc -and (Test-Path $cc)) { $picked = $cc; break }
    }

    if ($picked) {
      try { $picked = (Resolve-Path -LiteralPath $picked).Path } catch {}
      Update-DotEnv -Path $envPath -Updates @{ WORKFLOW_CONFIG_PATH = $picked }
      $envMap["WORKFLOW_CONFIG_PATH"] = $picked
      Write-Host ("WORKFLOW_CONFIG_PATH fixed -> " + $picked) -ForegroundColor Green
    } else {
      # Clear path so bot falls back to default (config/workflows.local.json)
      Update-DotEnv -Path $envPath -Updates @{ WORKFLOW_CONFIG_PATH = "" }
      $envMap["WORKFLOW_CONFIG_PATH"] = ""
      Write-Host "WORKFLOW_CONFIG_PATH not found, cleared to allow startup." -ForegroundColor Yellow
    }
  }
}

# --- Ensure workflows.local.json exists ---
try {
  Ensure-WorkflowsLocalConfig -RootDir $root
} catch {}

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
    Write-Host "Please close the existing process, or change CALLBACK_PORT in .env, then re-run." -ForegroundColor Yellow
    exit 1
  }
} catch {}

# --- Ensure required config ---
$configOk = Ensure-RequiredConfig -RootDir $root
if (-not $configOk) { exit 1 }

# Re-read .env after interactive config may have written new values
$envMap = Read-DotEnv -Path (Join-Path $root ".env")

Write-Host "Starting biaoge_bot ..." -ForegroundColor Cyan
$adminToken = ""
if ($envMap.ContainsKey("ADMIN_TOKEN")) {
  $adminToken = ($envMap["ADMIN_TOKEN"] -as [string]).Trim()
}
Write-Host ("Config page: http://" + $cbHost + ":" + $cbPort + "/admin/config?token=" + $adminToken) -ForegroundColor Cyan
Write-Host ""

& $venvPy -m biaoge_bot.main
