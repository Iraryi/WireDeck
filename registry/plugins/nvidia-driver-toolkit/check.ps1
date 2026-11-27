$ErrorActionPreference = 'Stop'

$ctx = if ($env:WIREDECK_HOOK_CONTEXT_FILE -and (Test-Path -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE)) {
    Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    @{}
}

$nv = if ($ctx.system_env -and $ctx.system_env.primary_nvidia_gpu) { $ctx.system_env.primary_nvidia_gpu } else { $null }
if (-not $nv -or -not $nv.name) {
    Write-Output (@{
        ok = $false
        status = 'warn'
        code = 'NO_NVIDIA_GPU'
        message = '当前未检测到 NVIDIA 独立显卡'
    } | ConvertTo-Json -Compress)
    exit 0
}

$parts = @("$($nv.name) · 驱动 $($nv.driver_version)")
if ($nv.supports_smooth_motion) {
    $parts += '支持 Smooth Motion（RTX 40/50 系）'
}
if ($ctx.system_env.is_dual_gpu) {
    $parts += '双显卡架构（推荐开独显锁定）'
}
if ($ctx.system_env.max_refresh_hz) {
    $parts += "屏幕 $($ctx.system_env.max_refresh_hz) Hz"
}
$latest = if ($ctx.plugin_settings -and $ctx.plugin_settings.latest_version) { [string]$ctx.plugin_settings.latest_version } else { '' }
if ($latest) {
    $parts += "最近检测到官方最新版 $latest"
}

Write-Output (@{
    ok = $true
    status = 'ok'
    code = 'NVIDIA_READY'
    message = ($parts -join ' · ')
} | ConvertTo-Json -Compress)
