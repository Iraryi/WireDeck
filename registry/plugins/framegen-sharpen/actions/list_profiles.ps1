$ErrorActionPreference = 'Stop'

function Get-LsXmlPath {
    if ($env:WIREDECK_LS_SETTINGS_XML -and (Test-Path -LiteralPath $env:WIREDECK_LS_SETTINGS_XML -PathType Leaf)) {
        return $env:WIREDECK_LS_SETTINGS_XML
    }
    $p = Join-Path $env:LOCALAPPDATA 'Lossless Scaling\Settings.xml'
    if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    return ''
}

function Convert-LsHotkey {
    param([string]$Key, [string]$Mods)
    $parts = @()
    if ($Mods) {
        $tokens = $Mods -split '\s+'
        if ($tokens -contains 'Control' -or $tokens -contains 'Ctrl') { $parts += 'Ctrl' }
        if ($tokens -contains 'Alt' -or $tokens -contains 'Menu') { $parts += 'Alt' }
        if ($tokens -contains 'Shift') { $parts += 'Shift' }
        if ($tokens -contains 'Windows' -or $tokens -contains 'Win') { $parts += 'Win' }
    }
    if ($Key) { $parts += $Key.Trim() }
    return ($parts -join '+')
}

$options = @(
    @{ value = '__auto__'; label = '自动匹配（使用小黄鸭自身规则）' }
)

$xmlPath = Get-LsXmlPath
$detectedHotkey = ''

if ($xmlPath) {
    try {
        $raw = [System.IO.File]::ReadAllText($xmlPath, [System.Text.Encoding]::UTF8)
        $doc = [xml]::new()
        $doc.LoadXml($raw)
        $detectedHotkey = Convert-LsHotkey -Key ([string]$doc.Settings.Hotkey) -Mods ([string]$doc.Settings.HotkeyModifierKeys)
        $profiles = @($doc.Settings.GameProfiles.Profile)
        foreach ($prof in $profiles) {
            if (-not $prof) { continue }
            $title = ([string]$prof.Title).Trim()
            if (-not $title) { continue }
            $fg = [string]$prof.FrameGeneration
            if (-not $fg) { $fg = 'Off' }
            $sc = [string]$prof.ScalingType
            if (-not $sc) { $sc = 'Off' }
            $cap = [string]$prof.CaptureApi
            if (-not $cap) { $cap = 'DXGI' }
            $autoTag = if ([string]$prof.AutoScale -eq 'true') { ' · AutoScale' } else { '' }
            $options += @{
                value = $title
                label = "$title [$fg · $sc · $cap$autoTag]"
            }
        }
    } catch {
        # Fallback cleanly with __auto__ only
    }
} else {
    $options += @{
        value = 'WireDeck 串流高刷推荐'
        label = '（未发现 Settings.xml，可点上方「生成推荐串流配置」）'
    }
}

Write-Output (@{
    ok = $true
    options = $options
    meta = @{
        xml_path = $xmlPath
        detected_hotkey = $detectedHotkey
    }
} | ConvertTo-Json -Depth 5 -Compress)
