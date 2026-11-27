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

function Resolve-EngineExe {
    param([string]$Engine, [string]$ConfiguredPath)
    if ($ConfiguredPath -and (Test-Path -LiteralPath $ConfiguredPath -PathType Leaf)) {
        return $ConfiguredPath
    }
    $lsRelPaths = @(
        'Program Files (x86)\Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe',
        'Program Files\Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe',
        'Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe',
        'SteamLibrary\steamapps\common\Lossless Scaling\LosslessScaling.exe',
        'Games\SteamLibrary\steamapps\common\Lossless Scaling\LosslessScaling.exe',
        'Game_Software\Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe'
    )
    $magpieRelPaths = @(
        'Magpie\Magpie.exe',
        'Program Files\Magpie\Magpie.exe',
        'Tools\Magpie\Magpie.exe'
    )
    $drives = @('C:', 'D:', 'E:', 'F:', 'G:')
    if ($Engine -eq 'magpie') {
        $localMagpie = Join-Path $env:LOCALAPPDATA 'Magpie\Magpie.exe'
        if (Test-Path -LiteralPath $localMagpie -PathType Leaf) { return $localMagpie }
        foreach ($d in $drives) {
            foreach ($r in $magpieRelPaths) {
                $cand = Join-Path ($d + '\') $r
                if (Test-Path -LiteralPath $cand -PathType Leaf) { return $cand }
            }
        }
        return ''
    }
    foreach ($d in $drives) {
        foreach ($r in $lsRelPaths) {
            $cand = Join-Path ($d + '\') $r
            if (Test-Path -LiteralPath $cand -PathType Leaf) { return $cand }
        }
    }
    return ''
}

$ctx = Get-Context
$settings = $ctx.plugin_settings
$preset = [string]$ctx.preset
$enabledPresets = @()
if ($settings -and $settings.enabled_presets) {
    $enabledPresets = @($settings.enabled_presets | ForEach-Object { [string]$_ })
}
if ($enabledPresets.Count -gt 0 -and ($enabledPresets -notcontains 'all') -and ($enabledPresets -notcontains $preset)) {
    Write-Output (@{ ok = $true; status = 'skipped'; code = 'PRESET_SKIPPED'; message = "当前预设 ($preset) 未启用补帧联动" } | ConvertTo-Json -Compress)
    exit 0
}

$hostCmds = @()
$keepAwake = $true
if ($settings -and ($null -ne $settings.keep_awake)) {
    $keepAwake = [bool]$settings.keep_awake
}
if ($keepAwake) {
    $hostCmds += @{ cmd = 'keep_awake'; enable = $true }
}

$autoLaunch = $true
if ($settings -and ($null -ne $settings.auto_launch_engine)) {
    $autoLaunch = [bool]$settings.auto_launch_engine
}
if (-not $autoLaunch) {
    $resOnly = @{ ok = $true; status = 'skipped'; code = 'AUTO_LAUNCH_OFF'; message = '已跳过自动后台启动增强器' }
    if ($hostCmds.Count -gt 0) { $resOnly['host_commands'] = $hostCmds }
    Write-Output ($resOnly | ConvertTo-Json -Depth 5 -Compress)
    exit 0
}

$engine = if ($settings -and $settings.engine) { [string]$settings.engine } else { 'lossless_scaling' }
$cfgPath = if ($settings -and $settings.exe_path) { [string]$settings.exe_path } else { '' }
$exe = Resolve-EngineExe -Engine $engine -ConfiguredPath $cfgPath

if (-not $exe) {
    Write-Output (@{ ok = $false; status = 'warn'; code = 'ENHANCER_NOT_FOUND'; message = '未找到 Lossless Scaling 或 Magpie，请在 WireDeck 插件设置中指定或自动扫描路径' } | ConvertTo-Json -Compress)
    exit 0
}

# Bind chosen Lossless Scaling Profile -> current streaming client path before launching
$xmlUpdated = $false
$selectedProfile = if ($settings -and $settings.ls_profile) { ([string]$settings.ls_profile).Trim() } else { '__auto__' }
$clientExePath = if ($ctx.client_exe_path) { ([string]$ctx.client_exe_path).Trim() } else { '' }
$xmlPath = Get-LsXmlPath

if ($engine -eq 'lossless_scaling' -and $selectedProfile -and $selectedProfile -ne '__auto__' -and $clientExePath -and $xmlPath -and (Test-Path -LiteralPath $xmlPath -PathType Leaf)) {
    try {
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
        foreach ($prof in @($doc.SelectNodes('//Settings/GameProfiles/Profile'))) {
            if ($prof -and ([string]$prof.Title).Trim() -eq $selectedProfile) {
                $pathNode = $prof.SelectSingleNode('Path')
                $curVal = if ($pathNode) { ([string]$pathNode.InnerText).Trim() } else { '' }
                if ($curVal -ne $clientExePath) {
                    if (-not $pathNode) {
                        $pathNode = $doc.CreateElement('Path')
                        $prof.AppendChild($pathNode) | Out-Null
                    }
                    $pathNode.InnerText = $clientExePath
                    $tmpXml = "$xmlPath.wiredeck.tmp"
                    $ws = [System.Xml.XmlWriterSettings]::new()
                    $ws.Encoding = [System.Text.UTF8Encoding]::new($false)
                    $ws.Indent = $true
                    $xw = [System.Xml.XmlWriter]::Create($tmpXml, $ws)
                    try { $doc.Save($xw) } finally { $xw.Close() }
                    Move-Item -LiteralPath $tmpXml -Destination $xmlPath -Force
                    $xmlUpdated = $true
                }
                break
            }
        }
    } catch {
        # Best effort binding; do not block launch
    }
}

$launchCmd = @{
    cmd = if ($xmlUpdated) { 'restart_process' } else { 'launch_process' }
    exe = $exe
    args = @()
    minimized = $true
}
if (-not $xmlUpdated) {
    $launchCmd['if_not_running'] = $true
}
$hostCmds += $launchCmd

$out = @{
    ok = $true
    status = 'ok'
    code = if ($xmlUpdated) { 'ENHANCER_PROFILE_BOUND' } else { 'ENHANCER_PRELAUNCH_READY' }
    message = if ($xmlUpdated) { "已将配置「$selectedProfile」绑定到串流程序并重载小黄鸭" } else { "已准备后台启动 $([System.IO.Path]::GetFileName($exe))" }
    host_commands = $hostCmds
}
if (-not $cfgPath) {
    $out['save_settings'] = @{ exe_path = $exe }
}
Write-Output ($out | ConvertTo-Json -Depth 6 -Compress)
