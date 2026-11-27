$ErrorActionPreference = 'Stop'

function Get-Context {
    if (-not $env:WIREDECK_HOOK_CONTEXT_FILE -or -not (Test-Path -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE)) {
        return @{}
    }
    return Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
}

$ctx = Get-Context
$settings = $ctx.plugin_settings
$autoClose = $false
if ($settings -and ($null -ne $settings.auto_close_on_exit)) {
    $autoClose = [bool]$settings.auto_close_on_exit
}
if (-not $autoClose) {
    Write-Output (@{ ok = $true; status = 'skipped'; code = 'KEEP_RUNNING'; message = '已保留增强器后台运行' } | ConvertTo-Json -Compress)
    exit 0
}

$engine = if ($settings -and $settings.engine) { [string]$settings.engine } else { 'lossless_scaling' }
$cfgPath = if ($settings -and $settings.exe_path) { [string]$settings.exe_path } else { '' }
$procName = if ($cfgPath) { [System.IO.Path]::GetFileName($cfgPath) } elseif ($engine -eq 'magpie') { 'Magpie.exe' } else { 'LosslessScaling.exe' }

$out = @{
    ok = $true
    status = 'ok'
    code = 'ENHANCER_STOP_REQUESTED'
    message = "正在结束 $procName"
    host_commands = @(
        @{ cmd = 'stop_process'; process_name = $procName }
    )
}
Write-Output ($out | ConvertTo-Json -Depth 5 -Compress)
