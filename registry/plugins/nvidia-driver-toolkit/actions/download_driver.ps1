$ErrorActionPreference = 'Stop'

$ctx = if ($env:WIREDECK_HOOK_CONTEXT_FILE -and (Test-Path -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE)) {
    Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    @{}
}

$settings = $ctx.plugin_settings
$url = if ($settings -and $settings.latest_url) { ([string]$settings.latest_url).Trim() } else { '' }

if (-not $url) {
    $checkRes = & (Join-Path $PSScriptRoot 'check_driver.ps1') | ConvertFrom-Json
    if (-not $checkRes.ok -or -not $checkRes.save_settings -or -not $checkRes.save_settings.latest_url) {
        Write-Output ($checkRes | ConvertTo-Json -Depth 5 -Compress)
        exit 0
    }
    $url = [string]$checkRes.save_settings.latest_url
}

$openAfter = $true
if ($settings -and ($null -ne $settings.auto_open_installer)) {
    $openAfter = [bool]$settings.auto_open_installer
}

$fileName = [System.IO.Path]::GetFileName(([uri]$url).AbsolutePath)
Write-Output (@{
    ok = $true
    status = 'ok'
    code = 'DRIVER_DOWNLOAD_STARTED'
    message = "已从 NVIDIA 官方通道下载 $fileName 到「下载」文件夹"
    host_commands = @(
        @{
            cmd = 'download_file'
            url = $url
            filename = $fileName
            open_after = $openAfter
        }
    )
} | ConvertTo-Json -Depth 5 -Compress)
