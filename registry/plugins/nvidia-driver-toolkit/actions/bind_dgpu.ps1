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
if ($ctx.client_exe_path -and (Test-Path -LiteralPath ([string]$ctx.client_exe_path) -PathType Leaf)) {
    $targets += [string]$ctx.client_exe_path
}
$enh = Find-LocalEnhancer
if ($enh) { $targets += $enh }

if ($targets.Count -eq 0) {
    Write-Output (@{
        ok = $false
        status = 'warn'
        code = 'NO_TARGET_EXE'
        message = '未检测到当前串流客户端或 Lossless Scaling / Magpie 的 .exe 路径'
    } | ConvertTo-Json -Compress)
    exit 0
}

$cmds = @()
foreach ($exe in ($targets | Select-Object -Unique)) {
    $cmds += @{ cmd = 'set_gpu_preference'; exe = $exe; mode = 'high_performance' }
}

$names = ($targets | Select-Object -Unique | ForEach-Object { [System.IO.Path]::GetFileName($_) }) -join ', '
Write-Output (@{
    ok = $true
    status = 'ok'
    code = 'DGPU_BOUND'
    message = "已将 $names 锁定为高性能 NVIDIA GPU"
    host_commands = $cmds
} | ConvertTo-Json -Depth 5 -Compress)
