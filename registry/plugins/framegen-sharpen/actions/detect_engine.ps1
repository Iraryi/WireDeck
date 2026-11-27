$ErrorActionPreference = 'Stop'

$foundLs = ''
$foundMagpie = ''

$lsRelPaths = @(
    'Program Files (x86)\Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe',
    'Program Files\Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe',
    'Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe',
    'SteamLibrary\steamapps\common\Lossless Scaling\LosslessScaling.exe',
    'Games\SteamLibrary\steamapps\common\Lossless Scaling\LosslessScaling.exe'
)
$drives = @('C:', 'D:', 'E:', 'F:', 'G:')
foreach ($d in $drives) {
    foreach ($r in $lsRelPaths) {
        $cand = Join-Path ($d + '\') $r
        if (-not $foundLs -and (Test-Path -LiteralPath $cand -PathType Leaf)) {
            $foundLs = $cand
        }
    }
}

$magpieCands = @(
    (Join-Path $env:LOCALAPPDATA 'Magpie\Magpie.exe'),
    'C:\Program Files\Magpie\Magpie.exe',
    'D:\Tools\Magpie\Magpie.exe'
)
foreach ($cand in $magpieCands) {
    if ($cand -and -not $foundMagpie -and (Test-Path -LiteralPath $cand -PathType Leaf)) {
        $foundMagpie = $cand
    }
}

if ($foundLs) {
    Write-Output (@{
        ok = $true
        status = 'ok'
        code = 'FOUND_LOSSLESS_SCALING'
        message = "已检测到 Lossless Scaling：$foundLs"
        save_settings = @{ engine = 'lossless_scaling'; exe_path = $foundLs }
    } | ConvertTo-Json -Depth 5 -Compress)
    exit 0
}

if ($foundMagpie) {
    Write-Output (@{
        ok = $true
        status = 'ok'
        code = 'FOUND_MAGPIE'
        message = "已检测到 Magpie：$foundMagpie"
        save_settings = @{ engine = 'magpie'; exe_path = $foundMagpie }
    } | ConvertTo-Json -Depth 5 -Compress)
    exit 0
}

Write-Output (@{
    ok = $false
    status = 'warn'
    code = 'ENHANCER_NOT_FOUND'
    message = '未自动扫描到 LosslessScaling.exe 或 Magpie.exe，请手动浏览选择程序路径'
} | ConvertTo-Json -Compress)
