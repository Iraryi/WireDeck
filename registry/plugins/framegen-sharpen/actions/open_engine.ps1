$ErrorActionPreference = 'Stop'

$ctx = if ($env:WIREDECK_HOOK_CONTEXT_FILE -and (Test-Path -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE)) {
    Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    @{}
}
$settings = $ctx.plugin_settings
$exe = if ($settings -and $settings.exe_path) { [string]$settings.exe_path } else { '' }
if (-not $exe -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    Write-Output (@{ ok = $false; status = 'warn'; code = 'NO_EXE_PATH'; message = '请先点击「自动扫描」或手动选择 LosslessScaling.exe / Magpie.exe' } | ConvertTo-Json -Compress)
    exit 0
}

Write-Output (@{
    ok = $true
    status = 'ok'
    code = 'OPENING_ENGINE'
    message = "正在打开 $([System.IO.Path]::GetFileName($exe)) 设置界面"
    host_commands = @(
        @{ cmd = 'launch_process'; exe = $exe; args = @(); minimized = $false; if_not_running = $false }
    )
} | ConvertTo-Json -Depth 5 -Compress)
