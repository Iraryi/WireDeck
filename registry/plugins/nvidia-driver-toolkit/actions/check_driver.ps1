$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ctx = if ($env:WIREDECK_HOOK_CONTEXT_FILE -and (Test-Path -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE)) {
    Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    @{}
}

$nvGpu = $null
if ($ctx.system_env -and $ctx.system_env.primary_nvidia_gpu) {
    $nvGpu = $ctx.system_env.primary_nvidia_gpu
}
if (-not $nvGpu -or -not $nvGpu.name) {
    Write-Output (@{
        ok = $false
        status = 'warn'
        code = 'NO_NVIDIA_GPU'
        message = '未检测到 NVIDIA 显卡（或已被系统禁用）'
    } | ConvertTo-Json -Compress)
    exit 0
}

$gpuName = [string]$nvGpu.name
$curVer = [string]$nvGpu.driver_version
$settings = $ctx.plugin_settings
$branch = if ($settings -and $settings.driver_branch) { [string]$settings.driver_branch } else { 'game_ready' }
$region = if ($settings -and $settings.download_region) { [string]$settings.download_region } else { 'auto' }
$upCRD = if ($branch -eq 'studio') { '1' } else { '0' }

$langCode = '1033'
if ($region -eq 'cn' -or ($region -eq 'auto' -and ([string]$ctx.language -like 'zh*'))) {
    $langCode = '2052'
}

try {
    $xmlResp = Invoke-RestMethod -Uri 'https://www.nvidia.com/Download/API/lookupValueSearch.aspx?TypeID=3' -TimeoutSec 12
    $normTarget = ($gpuName -replace '(?i)nvidia|geforce|\s+', '').ToLower()
    $best = $null
    $bestScore = -1
    foreach ($lv in @($xmlResp.LookupValueSearch.LookupValues.LookupValue)) {
        $n = [string]$lv.Name
        if (-not $n) { continue }
        $normCand = ($n -replace '(?i)nvidia|geforce|\s+', '').ToLower()
        $score = 0
        if ($n.Equals($gpuName, [System.StringComparison]::OrdinalIgnoreCase)) {
            $score = 100
        } elseif ($normCand -eq $normTarget) {
            $score = 90
        } elseif ($normCand.Contains($normTarget) -or $normTarget.Contains($normCand)) {
            $score = 60
        }
        if ($score -gt $bestScore) {
            $bestScore = $score
            $best = $lv
        }
    }

    if (-not $best -or $bestScore -lt 50) {
        Write-Output (@{
            ok = $false
            status = 'warn'
            code = 'GPU_NOT_MATCHED'
            message = "未能在 NVIDIA 官方产品库中精确匹配型号：$gpuName（当前驱动 $curVer）"
        } | ConvertTo-Json -Compress)
        exit 0
    }

    $pfid = [string]$best.Value
    $psid = [string]$best.ParentID
    $apiUrl = "https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/services/AjaxDriverService.php?func=DriverManualLookup&psid=$psid&pfid=$pfid&osID=57&languageCode=$langCode&beta=0&isWHQL=1&dltype=-1&dch=1&upCRD=$upCRD&qnf=0&sort1=0&numberOfResults=1"
    $drvResp = Invoke-RestMethod -Uri $apiUrl -TimeoutSec 12

    if (-not $drvResp -or -not $drvResp.IDS -or $drvResp.IDS.Count -eq 0) {
        Write-Output (@{
            ok = $false
            status = 'warn'
            code = 'NO_DRIVER_FOUND'
            message = "未查到 $gpuName 的 WHQL 驱动记录（当前本机 $curVer）"
        } | ConvertTo-Json -Compress)
        exit 0
    }

    $info = $drvResp.IDS[0].downloadInfo
    $latestVer = [string]$info.Version
    $dlUrl = [string]$info.DownloadURL
    $relDate = [string]$info.ReleaseDateTime
    $fsize = [string]$info.DownloadURLFileSize

    $isNewer = $false
    try {
        if ($curVer -and ([version]$latestVer -gt [version]$curVer)) { $isNewer = $true }
    } catch {
        $isNewer = ($latestVer -ne $curVer)
    }

    $msg = if ($isNewer) {
        "发现新版 NVIDIA 驱动 $latestVer（当前 $curVer · 发布 $relDate · 大小 $fsize），可直接点「下载官方驱动安装包」"
    } else {
        "当前已安装驱动 $curVer 已是最新（官方最新 $latestVer · $relDate）"
    }

    Write-Output (@{
        ok = $true
        status = 'ok'
        code = if ($isNewer) { 'DRIVER_UPDATE_AVAILABLE' } else { 'DRIVER_UP_TO_DATE' }
        message = $msg
        save_settings = @{
            latest_version = $latestVer
            latest_url = $dlUrl
        }
    } | ConvertTo-Json -Depth 5 -Compress)
} catch {
    Write-Output (@{
        ok = $false
        status = 'warn'
        code = 'NVIDIA_QUERY_FAILED'
        message = "在线查询 NVIDIA 驱动失败：$($_.Exception.Message)"
    } | ConvertTo-Json -Compress)
}
