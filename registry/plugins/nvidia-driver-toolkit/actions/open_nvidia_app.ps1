$ErrorActionPreference = 'Stop'

$cands = @(
    (Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVIDIA app\CEF\NVIDIA app.exe'),
    (Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVIDIA App\CEF\NVIDIA app.exe')
)
$exe = $cands | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1

if ($exe) {
    Write-Output (@{
        ok = $true
        status = 'ok'
        code = 'LAUNCHING_NV_APP'
        message = '正在打开 NVIDIA App（可在「图形 -> 程序设置」管理 Smooth Motion 与超低延迟）'
        host_commands = @(
            @{ cmd = 'launch_process'; exe = $exe; args = @(); minimized = $false; if_not_running = $false }
        )
    } | ConvertTo-Json -Depth 5 -Compress)
} else {
    Write-Output (@{
        ok = $true
        status = 'warn'
        code = 'NV_APP_MISSING'
        message = '本机未安装 NVIDIA App，已打开官方下载页面'
        host_commands = @(
            @{ cmd = 'open_url'; url = 'https://www.nvidia.com/software/nvidia-app/' }
        )
    } | ConvertTo-Json -Depth 5 -Compress)
}
