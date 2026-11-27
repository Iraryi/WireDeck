$ErrorActionPreference = 'Stop'

function Get-Context {
    if (-not $env:WIREDECK_HOOK_CONTEXT_FILE -or -not (Test-Path -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE)) {
        return @{}
    }
    return Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-LsXmlPath {
    if ($env:WIREDECK_LS_SETTINGS_XML -and (Test-Path -LiteralPath $env:WIREDECK_LS_SETTINGS_XML -PathType Leaf)) {
        return $env:WIREDECK_LS_SETTINGS_XML
    }
    $p = Join-Path $env:LOCALAPPDATA 'Lossless Scaling\Settings.xml'
    if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    return ''
}

function Convert-LsHotkey {
    param([string]$Key, [string]$Mods)
    $parts = @()
    if ($Mods) {
        $tokens = $Mods -split '\s+'
        if ($tokens -contains 'Control' -or $tokens -contains 'Ctrl') { $parts += 'Ctrl' }
        if ($tokens -contains 'Alt' -or $tokens -contains 'Menu') { $parts += 'Alt' }
        if ($tokens -contains 'Shift') { $parts += 'Shift' }
        if ($tokens -contains 'Windows' -or $tokens -contains 'Win') { $parts += 'Win' }
    }
    if ($Key) { $parts += $Key.Trim() }
    return ($parts -join '+')
}

$ctx = Get-Context
$settings = $ctx.plugin_settings
$preset = [string]$ctx.preset
$enabledPresets = @()
if ($settings -and $settings.enabled_presets) {
    $enabledPresets = @($settings.enabled_presets | ForEach-Object { [string]$_ })
}
if ($enabledPresets.Count -gt 0 -and ($enabledPresets -notcontains 'all') -and ($enabledPresets -notcontains $preset)) {
    Write-Output (@{ ok = $true; status = 'skipped'; code = 'PRESET_SKIPPED'; message = "当前预设 ($preset) 未启用补帧自动触发" } | ConvertTo-Json -Compress)
    exit 0
}

$triggerMode = if ($settings -and $settings.trigger_mode) { ([string]$settings.trigger_mode).Trim() } else { 'smart' }
$autoTrigger = $true
if ($settings -and ($null -ne $settings.auto_trigger_scale)) {
    $autoTrigger = [bool]$settings.auto_trigger_scale
}
if (-not $autoTrigger -or $triggerMode -eq 'launch_only') {
    Write-Output (@{ ok = $true; status = 'skipped'; code = 'AUTO_TRIGGER_OFF'; message = '已按策略跳过发送补帧热键' } | ConvertTo-Json -Compress)
    exit 0
}

$engine = if ($settings -and $settings.engine) { [string]$settings.engine } else { 'lossless_scaling' }
$selectedProfile = if ($settings -and $settings.ls_profile) { ([string]$settings.ls_profile).Trim() } else { '__auto__' }
$clientPath = if ($ctx.client_exe_path) { ([string]$ctx.client_exe_path).Trim() } else { '' }
$clientExe = if ($ctx.client_exe) { ([string]$ctx.client_exe).Trim() } else { '' }

$xmlHotkey = ''
$autoscaleHandled = $false

if ($engine -eq 'lossless_scaling') {
    $xmlPath = Get-LsXmlPath
    if ($xmlPath) {
        try {
            $raw = [System.IO.File]::ReadAllText($xmlPath, [System.Text.Encoding]::UTF8)
            $doc = [xml]::new()
            $doc.LoadXml($raw)
            $xmlHotkey = Convert-LsHotkey -Key ([string]$doc.Settings.Hotkey) -Mods ([string]$doc.Settings.HotkeyModifierKeys)
            if ($triggerMode -eq 'smart') {
                foreach ($prof in @($doc.Settings.GameProfiles.Profile)) {
                    if (-not $prof) { continue }
                    if ([string]$prof.AutoScale -ne 'true') { continue }
                    $title = ([string]$prof.Title).Trim()
                    $pp = ([string]$prof.Path).Trim()
                    $pathMatched = ($pp -and (($clientPath -and $pp.Equals($clientPath, [System.StringComparison]::OrdinalIgnoreCase)) -or ($clientExe -and $pp.EndsWith($clientExe, [System.StringComparison]::OrdinalIgnoreCase))))
                    if (($selectedProfile -ne '__auto__' -and $title -eq $selectedProfile -and $pathMatched) -or ($selectedProfile -eq '__auto__' -and $pathMatched)) {
                        $autoscaleHandled = $true
                        break
                    }
                }
            }
        } catch {
            # Ignore parse error and fallback to configured/default hotkey
        }
    }
}

if ($autoscaleHandled) {
    Write-Output (@{
        ok = $true
        status = 'ok'
        code = 'AUTOSCALE_HANDLED'
        message = '小黄鸭已对该程序启用 AutoScale，已智能跳过重复热键，防止开关抵消'
    } | ConvertTo-Json -Compress)
    exit 0
}

$customHotkey = if ($settings -and $settings.scale_hotkey) { ([string]$settings.scale_hotkey).Trim() } else { '' }
$hotkey = if ($customHotkey) { $customHotkey } elseif ($engine -eq 'lossless_scaling' -and $xmlHotkey) { $xmlHotkey } elseif ($engine -eq 'magpie') { 'Win+Shift+A' } else { 'Ctrl+Alt+S' }

$delaySec = 2.0
if ($settings -and ($null -ne $settings.delay_sec)) {
    try { $delaySec = [double]$settings.delay_sec } catch { $delaySec = 2.0 }
}
if ($delaySec -lt 0.2) { $delaySec = 0.2 }
if ($delaySec -gt 15.0) { $delaySec = 15.0 }

$waitCmd = @{
    cmd = 'wait_window'
    timeout_sec = 15
}
if ($ctx.client_pid -and ([int]$ctx.client_pid -gt 0)) {
    $waitCmd['pid'] = [int]$ctx.client_pid
}
if ($ctx.client_exe) {
    $waitCmd['fallback_process_name'] = [string]$ctx.client_exe
}

$focusCmd = @{ cmd = 'focus_window' }
if ($ctx.client_exe) {
    $focusCmd['process_name'] = [string]$ctx.client_exe
}

$out = @{
    ok = $true
    status = 'ok'
    code = 'SCALE_TRIGGER_DISPATCHED'
    message = "等待串流窗口就绪后自动触发 $hotkey"
    host_commands = @(
        $waitCmd,
        @{ cmd = 'sleep'; seconds = $delaySec },
        $focusCmd,
        @{ cmd = 'send_hotkey'; keys = $hotkey }
    )
}
Write-Output ($out | ConvertTo-Json -Depth 6 -Compress)
