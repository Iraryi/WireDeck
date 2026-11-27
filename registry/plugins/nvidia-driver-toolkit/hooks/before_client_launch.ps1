$ErrorActionPreference = 'Stop'

function Find-LocalEnhancer {
    $lsRelPaths = @(
        'Program Files (x86)\Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe',
        'Program Files\Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe',
        'Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe',
        'SteamLibrary\steamapps\common\Lossless Scaling\LosslessScaling.exe',
        'Games\SteamLibrary\steamapps\common\Lossless Scaling\LosslessScaling.exe',
        'Game_Software\Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe'
    )
    foreach ($d in @('C:', 'D:', 'E:', 'F:', 'G:')) {
        foreach ($r in $lsRelPaths) {
            $c = Join-Path ($d + '\') $r
            if (Test-Path -LiteralPath $c -PathType Leaf) { return $c }
        }
    }
    return ''
}

$ctx = if ($env:WIREDECK_HOOK_CONTEXT_FILE -and (Test-Path -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE)) {
    Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    @{}
}

$settings = $ctx.plugin_settings
$bindClient = $true
if ($settings -and ($null -ne $settings.auto_bind_dgpu)) { $bindClient = [bool]$settings.auto_bind_dgpu }
$bindEnh = $true
if ($settings -and ($null -ne $settings.bind_enhancer_dgpu)) { $bindEnh = [bool]$settings.bind_enhancer_dgpu }

if (-not $bindClient -and -not $bindEnh) {
    Write-Output (@{ ok = $true; status = 'skipped'; code = 'BIND_SKIPPED'; message = '已跳过自动独显绑定' } | ConvertTo-Json -Compress)
    exit 0
}

$cmds = @()
if ($bindClient -and $ctx.client_exe_path) {
    $cmds += @{ cmd = 'set_gpu_preference'; exe = [string]$ctx.client_exe_path; mode = 'high_performance' }
}
if ($bindEnh) {
    $enh = Find-LocalEnhancer
    if ($enh) {
        $cmds += @{ cmd = 'set_gpu_preference'; exe = $enh; mode = 'high_performance' }
    }
}

if ($cmds.Count -eq 0) {
    Write-Output (@{ ok = $true; status = 'skipped'; code = 'NO_TARGET_TO_BIND'; message = '未发现需要绑定独显的程序路径' } | ConvertTo-Json -Compress)
    exit 0
}

Write-Output (@{
    ok = $true
    status = 'ok'
    code = 'DGPU_PREBIND_OK'
    message = "已确保 $($cmds.Count) 个相关程序运行在高性能 GPU 上"
    host_commands = $cmds
} | ConvertTo-Json -Depth 5 -Compress)
