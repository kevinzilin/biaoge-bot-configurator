$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

Add-Type -AssemblyName System.Net.Http

$feishuAppId = $env:FEISHU_APP_ID
$feishuAppSecret = $env:FEISHU_APP_SECRET
$bitableAppToken = $env:BITABLE_APP_TOKEN
$bitableTableId = $env:BITABLE_TABLE_ID
$comfyBaseUrl = $env:COMFYUI_BASE_URL

if (-not $feishuAppId -or -not $feishuAppSecret -or -not $bitableAppToken -or -not $bitableTableId -or -not $comfyBaseUrl) {
  throw "missing required env vars"
}

$workflowName = "klein添加真实细节"
$fieldStatus = "任务状态"
$fieldImages = "参考图"
$fieldOutput = "生成结果"
$fieldPromptId = "任务ID"
$fieldCreated = "创建时间"

$statusQueued = "待处理"
$statusRunning = "处理中"
$statusDone = "已完成"
$statusFailed = "生成失败"

$tmpDir = Join-Path $PSScriptRoot "..\\tmp\\closed_loop"
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

function Get-TenantToken {
  $url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
  $body = @{ app_id = $feishuAppId; app_secret = $feishuAppSecret } | ConvertTo-Json -Depth 5
  $res = Invoke-RestMethod -Method Post -Uri $url -ContentType "application/json" -Body $body -TimeoutSec 20
  if (-not $res.tenant_access_token) { throw "auth failed" }
  return $res.tenant_access_token
}

function Invoke-Feishu {
  param(
    [Parameter(Mandatory = $true)] [string] $Method,
    [Parameter(Mandatory = $true)] [string] $Url,
    [Parameter()] $Body,
    [Parameter(Mandatory = $true)] [string] $Token
  )
  $headers = @{ Authorization = "Bearer $Token" }
  if ($Body -ne $null) {
    $json = $Body | ConvertTo-Json -Depth 30
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    return Invoke-RestMethod -Method $Method -Uri $Url -Headers $headers -ContentType "application/json; charset=utf-8" -Body $bytes -TimeoutSec 30
  }
  return Invoke-RestMethod -Method $Method -Uri $Url -Headers $headers -TimeoutSec 30
}

function Search-QueuedRecords {
  param([Parameter(Mandatory = $true)] [string] $Token)
  $url = "https://open.feishu.cn/open-apis/bitable/v1/apps/$bitableAppToken/tables/$bitableTableId/records/search"
  $body = @{
    page_size = 100
    filter = @{
      conjunction = "and"
      conditions = @(@{ field_name = $fieldStatus; operator = "is"; value = @($statusQueued) })
    }
    sort = @(@{ field_name = $fieldCreated; desc = $false })
  }
  $res = Invoke-Feishu -Method Post -Url $url -Body $body -Token $Token
  return @($res.data.items)
}

function Update-RecordFields {
  param(
    [Parameter(Mandatory = $true)] [string] $Token,
    [Parameter(Mandatory = $true)] [string] $RecordId,
    [Parameter(Mandatory = $true)] [hashtable] $Fields
  )
  $url = "https://open.feishu.cn/open-apis/bitable/v1/apps/$bitableAppToken/tables/$bitableTableId/records/$RecordId"
  $body = @{ fields = $Fields }
  $res = Invoke-Feishu -Method Put -Url $url -Body $body -Token $Token
  if ($res.code -ne 0 -and $res.code -ne $null) { throw ("update record failed: " + ($res | ConvertTo-Json -Depth 10)) }
}

function Download-FeishuMedia {
  param(
    [Parameter(Mandatory = $true)] [string] $Token,
    [Parameter(Mandatory = $true)] [string] $FileToken,
    [Parameter(Mandatory = $true)] [string] $FileName
  )
  $safe = ($FileName -replace '[\\/:*?"<>|\r\n]+', "_")
  $out = Join-Path $tmpDir $safe
  $url = "https://open.feishu.cn/open-apis/drive/v1/medias/$FileToken/download"
  $headers = @{ Authorization = "Bearer $Token" }
  Invoke-WebRequest -Method Get -Uri $url -Headers $headers -TimeoutSec 120 -OutFile $out | Out-Null
  return $out
}

function Upload-ComfyImage {
  param(
    [Parameter(Mandatory = $true)] [string] $FilePath,
    [Parameter(Mandatory = $true)] [string] $ComfyUrlBase
  )
  $name = Split-Path -Leaf $FilePath
  $bytes = [IO.File]::ReadAllBytes($FilePath)
  $client = [System.Net.Http.HttpClient]::new()
  $mp = [System.Net.Http.MultipartFormDataContent]::new()
  $fc = [System.Net.Http.ByteArrayContent]::new($bytes)
  $fc.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("application/octet-stream")
  $mp.Add($fc, "image", $name)
  $mp.Add([System.Net.Http.StringContent]::new("input"), "type")
  $mp.Add([System.Net.Http.StringContent]::new("true"), "overwrite")
  $resp = $client.PostAsync("$ComfyUrlBase/upload/image", $mp).Result
  $code = [int]$resp.StatusCode
  $text = $resp.Content.ReadAsStringAsync().Result
  if ($code -ne 200) { throw "upload comfy failed: $code $text" }
  $obj = $text | ConvertFrom-Json
  if ($obj.subfolder -and $obj.subfolder.Length -gt 0) { return ($obj.subfolder + "/" + $obj.name) }
  return $obj.name
}

function Queue-WorkflowPrompt {
  param(
    [Parameter(Mandatory = $true)] [string] $ComfyUrlBase,
    [Parameter(Mandatory = $true)] [string] $WorkflowName,
    [Parameter(Mandatory = $true)] $NodeInfoList
  )
  $obj = @{ workflowName = $WorkflowName; nodeInfoList = $NodeInfoList; client_id = "biaoge-closed-loop" }
  $json = $obj | ConvertTo-Json -Depth 20
  $client = [System.Net.Http.HttpClient]::new()
  $content = [System.Net.Http.StringContent]::new($json, [System.Text.Encoding]::UTF8, "application/json")
  $resp = $client.PostAsync("$ComfyUrlBase/prompt_workflow", $content).Result
  $code = [int]$resp.StatusCode
  $text = $resp.Content.ReadAsStringAsync().Result
  if ($code -ne 200) { throw "queue failed: $code $text" }
  $res = $text | ConvertFrom-Json
  return $res.prompt_id
}

function Wait-History {
  param(
    [Parameter(Mandatory = $true)] [string] $ComfyUrlBase,
    [Parameter(Mandatory = $true)] [string] $PromptId
  )
  $deadline = (Get-Date).AddMinutes(20)
  while ((Get-Date) -lt $deadline) {
    try {
      $res = Invoke-RestMethod -Method Get -Uri "$ComfyUrlBase/history/$PromptId" -TimeoutSec 10
      $outs = Extract-Outputs -History $res
      if ($outs.Count -gt 0) { return $res }
    } catch {
      Start-Sleep -Seconds 2
    }
    Start-Sleep -Seconds 2
  }
  throw "history timeout: $PromptId"
}

function Extract-Outputs {
  param([Parameter(Mandatory = $true)] $History)
  $outputs = @()
  foreach ($k in $History.PSObject.Properties.Name) {
    $item = $History.$k
    if (-not $item -or -not $item.outputs) { continue }
    foreach ($nodeId in $item.outputs.PSObject.Properties.Name) {
      $out = $item.outputs.$nodeId
      foreach ($key in @("images", "gifs", "videos", "files")) {
        if ($out.$key) {
          foreach ($f in $out.$key) { $outputs += $f }
        }
      }
    }
  }
  return $outputs
}

function Download-ComfyFile {
  param(
    [Parameter(Mandatory = $true)] [string] $ComfyUrlBase,
    [Parameter(Mandatory = $true)] $FileObj
  )
  $filename = $FileObj.filename
  $subfolder = $FileObj.subfolder
  $type = $FileObj.type
  $safe = ($filename -replace '[\\/:*?"<>|\r\n]+', "_")
  $out = Join-Path $tmpDir $safe
  $qs = "filename=$([uri]::EscapeDataString($filename))&subfolder=$([uri]::EscapeDataString($subfolder))&type=$([uri]::EscapeDataString($type))"
  Invoke-WebRequest -Method Get -Uri "$ComfyUrlBase/view?$qs" -TimeoutSec 120 -OutFile $out | Out-Null
  return $out
}

function Upload-FeishuToBitable {
  param(
    [Parameter(Mandatory = $true)] [string] $Token,
    [Parameter(Mandatory = $true)] [string] $FilePath
  )
  $name = Split-Path -Leaf $FilePath
  $bytes = [IO.File]::ReadAllBytes($FilePath)
  $size = $bytes.Length
  if ($size -le 0) { throw "empty file" }
  $ext = [IO.Path]::GetExtension($name)
  if ($ext -eq $null) { $ext = "" }
  $ext = $ext.ToLowerInvariant()
  $asImage = $ext -in @(".png", ".jpg", ".jpeg", ".webp", ".gif")
  $parentType = $(if ($asImage) { "bitable_image" } else { "bitable_file" })

  $client = [System.Net.Http.HttpClient]::new()
  $client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $Token)
  $mp = [System.Net.Http.MultipartFormDataContent]::new()
  $mp.Add([System.Net.Http.StringContent]::new($name), "file_name")
  $mp.Add([System.Net.Http.StringContent]::new($parentType), "parent_type")
  $mp.Add([System.Net.Http.StringContent]::new($bitableAppToken), "parent_node")
  $mp.Add([System.Net.Http.StringContent]::new([string]$size), "size")
  $fc = [System.Net.Http.ByteArrayContent]::new($bytes)
  $fc.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("application/octet-stream")
  $mp.Add($fc, "file", $name)

  $resp = $client.PostAsync("https://open.feishu.cn/open-apis/drive/v1/medias/upload_all", $mp).Result
  $code = [int]$resp.StatusCode
  $text = $resp.Content.ReadAsStringAsync().Result
  if ($code -ne 200) { throw "feishu upload_all failed: $code $text" }
  $obj = $text | ConvertFrom-Json
  if ($obj.code -ne 0 -and $obj.code -ne $null) { throw "feishu upload_all failed: $text" }
  return $obj.data.file_token
}

$token = Get-TenantToken
$records = Search-QueuedRecords -Token $token
"queued_records=" + $records.Count

$limit = 0
if ($env:CLOSED_LOOP_LIMIT) {
  try { $limit = [int]$env:CLOSED_LOOP_LIMIT } catch { $limit = 0 }
}
if ($limit -gt 0 -and $records.Count -gt $limit) {
  $records = $records[0..($limit - 1)]
  "limit=" + $limit
}

foreach ($rec in $records) {
  $rid = $rec.record_id
  "processing=" + $rid

  try {
    Update-RecordFields -Token $token -RecordId $rid -Fields @{ $fieldStatus = $statusRunning }

    $imgs = @()
    $raw = $rec.fields.$fieldImages
    if ($raw -is [System.Collections.IEnumerable]) {
      foreach ($it in $raw) {
        if ($it.file_token) { $imgs += $it }
      }
    } elseif ($raw -and $raw.file_token) {
      $imgs += $raw
    }

    if ($imgs.Count -lt 1) {
      Update-RecordFields -Token $token -RecordId $rid -Fields @{ $fieldStatus = $statusFailed }
      continue
    }

    $uploaded = @()
    foreach ($it in $imgs) {
      $path = Download-FeishuMedia -Token $token -FileToken $it.file_token -FileName $it.name
      $uploaded += (Upload-ComfyImage -FilePath $path -ComfyUrlBase $comfyBaseUrl)
      if ($uploaded.Count -ge 2) { break }
    }

    $nodeInfo = @(@{ nodeId = "448"; fieldName = "image"; fieldValue = $uploaded[0] })
    if ($uploaded.Count -ge 2) { $nodeInfo += @{ nodeId = "481"; fieldName = "image"; fieldValue = $uploaded[1] } }

    $promptId = Queue-WorkflowPrompt -ComfyUrlBase $comfyBaseUrl -WorkflowName $workflowName -NodeInfoList $nodeInfo
    Update-RecordFields -Token $token -RecordId $rid -Fields @{ $fieldPromptId = $promptId }

    $history = Wait-History -ComfyUrlBase $comfyBaseUrl -PromptId $promptId
    $outs = Extract-Outputs -History $history
    if ($outs.Count -lt 1) { throw "no outputs" }

    $fileTokens = @()
    foreach ($fo in $outs) {
      $local = Download-ComfyFile -ComfyUrlBase $comfyBaseUrl -FileObj $fo
      $ft = Upload-FeishuToBitable -Token $token -FilePath $local
      $fileTokens += @{ file_token = $ft; name = (Split-Path -Leaf $local) }
    }

    Update-RecordFields -Token $token -RecordId $rid -Fields @{
      $fieldOutput = $fileTokens
      $fieldStatus = $statusDone
    }
  } catch {
    try { Update-RecordFields -Token $token -RecordId $rid -Fields @{ $fieldStatus = $statusFailed } } catch {}
    "failed=" + $rid
    ("error=" + ($_.Exception.Message -replace "\r?\n", " "))
  }
}
