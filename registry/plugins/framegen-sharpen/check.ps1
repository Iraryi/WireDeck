$ErrorActionPreference = 'Stop'

$ctx = if ($env:WIREDECK_HOOK_CONTEXT_FILE -and (Test-Path -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE)) {
    Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    @{}
}

function Get-LsXmlPath {
    if ($env:WIREDECK_LS_SETTINGS_XML -and (Test-Path -LiteralPath $env:WIREDECK_LS_SETTINGS_XML -PathType Leaf)) {
        return $env:WIREDECK_LS_SETTINGS_XML
    }
    $p = Join-Path $env:LOCALAPPDATA 'Lossless Scaling\Settings.xml'
    if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    return ''
}

$settings = $ctx.plugin_settings
$exe = if ($settings -and $settings.exe_path) { [string]$settings.exe_path } else { '' }
$engine = if ($settings -and $settings.engine) { [string]$settings.engine } else { 'lossless_scaling' }

if (-not $exe -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    Write-Output (@{
        ok = $false
        status = 'warn'
        code = 'EXE_MISSING'
        message = '尚未配置增强程序路径（点击「自动扫描小黄鸭 / Magpie」即可）'
    } | ConvertTo-Json -Compress)
    exit 0
}

$notes = @()
$status = 'ok'

if ($ctx.system_env) {
    if ($ctx.system_env.max_refresh_hz) {
        $notes += "屏幕 ${($ctx.system_env.max_refresh_hz)} Hz"
    }
    if ($ctx.system_env.on_battery) {
        $notes += '当前使用电池（高刷补帧建议插电）'
        $status = 'warn'
    }
}

if ($engine -eq 'lossless_scaling') {
    $xmlPath = Get-LsXmlPath
    if ($xmlPath) {
        try {
            $raw = [System.IO.File]::ReadAllText($xmlPath, [System.Text.Encoding]::UTF8)
            $doc = [xml]::new()
            $doc.LoadXml($raw)
            $hk = ("$($doc.Settings.HotkeyModifierKeys) $($doc.Settings.Hotkey)").Trim()
            if ($hk) { $notes += "热键 $hk" }

            $sel = if ($settings -and $settings.ls_profile) { ([string]$settings.ls_profile).Trim() } else { '__auto__' }
            if ($sel -and $sel -ne '__auto__') {
                $matched = @($doc.Settings.GameProfiles.Profile) | Where-Object { $_ -and ([string]$_.Title).Trim() -eq $sel } | Select-Object -First 1
                if ($matched) {
                    $fg = [string]$matched.FrameGeneration
                    $sc = [string]$matched.ScalingType
                    $cap = [string]$matched.CaptureApi
                    $notes += "配置「$sel」($fg/$sc/$cap)"
                    if (($fg -eq 'Off' -or -not $fg) -and ($sc -eq 'Off' -or -not $sc)) {
                        $notes += '⚠ 该配置未开补帧或锐化'
                        $status = 'warn'
                    }
                    if ($ctx.system_env -and $ctx.system_env.is_dual_gpu -and $cap -eq 'DXGI') {
                        $notes += '⚠ 双显卡笔记本建议将捕获改为 WGC'
                        $status = 'warn'
                    }
                } else {
                    $notes += "⚠ Settings.xml 中未找到「$sel」"
                    $status = 'warn'
                }
            }
        } catch {
            $notes += 'Settings.xml 解析失败'
        }
    } else {
        $notes += '未检测到 Settings.xml'
    }
}

$procName = [System.IO.Path]::GetFileNameWithoutExtension($exe)
$running = $null -ne (Get-Process -Name $procName -ErrorAction SilentlyContinue | Select-Object -First 1)
$runTag = if ($running) { '运行中' } else { '待命' }
$summary = ($notes + $runTag) -join ' · '

Write-Output (@{
    ok = $true
    status = $status
    code = 'ENGINE_READY'
    message = $summary
} | ConvertTo-Json -Compress)
