$ErrorActionPreference = 'Stop'

$ctx = if ($env:WIREDECK_HOOK_CONTEXT_FILE -and (Test-Path -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE)) {
    Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    @{}
}
$settings = $ctx.plugin_settings
$engine = if ($settings -and $settings.engine) { [string]$settings.engine } else { 'lossless_scaling' }
$customHotkey = if ($settings -and $settings.scale_hotkey) { ([string]$settings.scale_hotkey).Trim() } else { '' }
$hotkey = if ($customHotkey) { $customHotkey } elseif ($engine -eq 'magpie') { 'Win+Shift+A' } else { 'Ctrl+Alt+S' }

Write-Output (@{
    ok = $true
    status = 'ok'
    code = 'HOTKEY_TRIGGERED'
    message = "将在 1.5 秒后发送测试热键：$hotkey"
    host_commands = @(
        @{ cmd = 'sleep'; seconds = 1.5 },
        @{ cmd = 'send_hotkey'; keys = $hotkey }
    )
} | ConvertTo-Json -Depth 5 -Compress)
