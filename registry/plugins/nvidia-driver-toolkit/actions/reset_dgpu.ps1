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
    $m = Join-Path $env:LOCALAPPDATA 'Magpie\Magpie.exe'
    if (Test-Path -LiteralPath $m -PathType Leaf) { return $m }
    return ''
}

$ctx = if ($env:WIREDECK_HOOK_CONTEXT_FILE -and (Test-Path -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE)) {
    Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    @{}
}

$targets = @()
if ($ctx.client_exe_path) { $targets += [string]$ctx.client_exe_path }
$enh = Find-LocalEnhancer
if ($enh) { $targets += $enh }

if ($targets.Count -eq 0) {
    Write-Output (@{ ok = $true; status = 'ok'; code = 'NOTHING_TO_RESET'; message = '暂无需要还原的应用偏好' } | ConvertTo-Json -Compress)
    exit 0
}

$cmds = @()
foreach ($exe in ($targets | Select-Object -Unique)) {
    $cmds += @{ cmd = 'set_gpu_preference'; exe = $exe; mode = 'default' }
}

Write-Output (@{
    ok = $true
    status = 'ok'
    code = 'DGPU_RESET'
    message = '已还原 Windows 默认 GPU 调度偏好'
    host_commands = $cmds
} | ConvertTo-Json -Depth 5 -Compress)
