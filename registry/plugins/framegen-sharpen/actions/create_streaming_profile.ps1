$ErrorActionPreference = 'Stop'

function Get-Context {
    if (-not $env:WIREDECK_HOOK_CONTEXT_FILE -or -not (Test-Path -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE)) {
        return @{}
    }
    return Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-LsXmlPath {
    if ($env:WIREDECK_LS_SETTINGS_XML) { return $env:WIREDECK_LS_SETTINGS_XML }
    return (Join-Path $env:LOCALAPPDATA 'Lossless Scaling\Settings.xml')
}

$ctx = Get-Context
$xmlPath = Get-LsXmlPath
if (-not (Test-Path -LiteralPath $xmlPath -PathType Leaf)) {
    Write-Output (@{
        ok = $false
        status = 'warn'
        code = 'LS_XML_NOT_FOUND'
        message = '未找到小黄鸭 Settings.xml，请先运行一次 Lossless Scaling 让其生成初始配置'
    } | ConvertTo-Json -Compress)
    exit 0
}

# Backup original Settings.xml once
$dataDir = if ($ctx.data_dir) { [string]$ctx.data_dir } elseif ($env:WIREDECK_DATA_DIR) { $env:WIREDECK_DATA_DIR } else { Split-Path $xmlPath -Parent }
$backupDir = Join-Path $dataDir 'runtime'
if (-not (Test-Path -LiteralPath $backupDir)) { New-Item -ItemType Directory -Path $backupDir -Force | Out-Null }
$backupFile = Join-Path $backupDir 'ls_settings_backup.xml'
if (-not (Test-Path -LiteralPath $backupFile)) {
    Copy-Item -LiteralPath $xmlPath -Destination $backupFile -Force
}

$raw = [System.IO.File]::ReadAllText($xmlPath, [System.Text.Encoding]::UTF8)
$doc = [xml]::new()
$doc.LoadXml($raw)

$gp = $doc.SelectSingleNode('//Settings/GameProfiles')
if (-not $gp) {
    Write-Output (@{ ok = $false; status = 'fail'; code = 'INVALID_LS_XML'; message = 'Settings.xml 缺少 GameProfiles 节点' } | ConvertTo-Json -Compress)
    exit 0
}

$targetTitle = 'WireDeck 串流高刷推荐'
$targetHz = 120
if ($ctx.system_env -and $ctx.system_env.max_refresh_hz -and ([int]$ctx.system_env.max_refresh_hz -gt 60)) {
    $targetHz = [int]$ctx.system_env.max_refresh_hz
}
$clientPath = if ($ctx.client_exe_path) { [string]$ctx.client_exe_path } else { '' }

$existing = $null
foreach ($node in @($gp.SelectNodes('Profile'))) {
    if ($node -and ([string]$node.Title).Trim() -eq $targetTitle) {
        $existing = $node
        break
    }
}

if (-not $existing) {
    $tpl = $gp.SelectSingleNode('Profile')
    if (-not $tpl) {
        Write-Output (@{ ok = $false; status = 'fail'; code = 'NO_TEMPLATE_PROFILE'; message = 'Settings.xml 中没有任何基础 Profile 模板' } | ConvertTo-Json -Compress)
        exit 0
    }
    $existing = $tpl.CloneNode($true)
    $gp.AppendChild($existing) | Out-Null
}

function Set-XmlChildText {
    param($Parent, [string]$Tag, [string]$Value)
    $child = $Parent.SelectSingleNode($Tag)
    if (-not $child) {
        $child = $Parent.OwnerDocument.CreateElement($Tag)
        $Parent.AppendChild($child) | Out-Null
    }
    $child.InnerText = $Value
}

Set-XmlChildText $existing 'Title' $targetTitle
Set-XmlChildText $existing 'Path' $clientPath
Set-XmlChildText $existing 'AutoScale' 'false'
Set-XmlChildText $existing 'ScalingMode' 'Custom'
Set-XmlChildText $existing 'ScalingFitMode' 'AspectRatio'
Set-XmlChildText $existing 'ScaleFactor' '1'
Set-XmlChildText $existing 'ResizeBeforeScaling' 'true'
Set-XmlChildText $existing 'ScalingType' 'LS1'
Set-XmlChildText $existing 'LS1Type' 'BALANCED'
Set-XmlChildText $existing 'LS1Sharpness' '1'
Set-XmlChildText $existing 'FrameGeneration' 'LSFG3'
Set-XmlChildText $existing 'LSFG3Mode1' 'FIXED'
Set-XmlChildText $existing 'LSFG3Multiplier' '2'
Set-XmlChildText $existing 'LSFG3Target' ([string]$targetHz)
Set-XmlChildText $existing 'CaptureApi' 'WGC'
Set-XmlChildText $existing 'MaxFrameLatency' '1'
Set-XmlChildText $existing 'SyncMode' 'OFF'

$tmpXml = "$xmlPath.wiredeck.tmp"
$settingsXml = [System.Xml.XmlWriterSettings]::new()
$settingsXml.Encoding = [System.Text.UTF8Encoding]::new($false)
$settingsXml.Indent = $true
$writer = [System.Xml.XmlWriter]::Create($tmpXml, $settingsXml)
try {
    $doc.Save($writer)
} finally {
    $writer.Close()
}
Move-Item -LiteralPath $tmpXml -Destination $xmlPath -Force

Write-Output (@{
    ok = $true
    status = 'ok'
    code = 'STREAM_PROFILE_CREATED'
    message = "已写入「$targetTitle」（WGC 捕获 · LSFG3 2x 目标 ${targetHz}Hz · LS1 锐化），并已选定为当前串流配置"
    save_settings = @{ ls_profile = $targetTitle; engine = 'lossless_scaling' }
} | ConvertTo-Json -Depth 5 -Compress)
