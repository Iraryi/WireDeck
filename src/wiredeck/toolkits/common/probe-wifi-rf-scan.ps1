$raw = (& netsh wlan show networks mode=bssid 2>$null) -join "`n"
$channels = @{}
foreach ($line in ($raw -split "`r?`n")) {
  if ($line -match '(?:Channel|频道|頻道)\s*:\s*(\d+)') {
    $ch = [int]$matches[1]
    $key = if ($ch -le 14) { "2.4G_ch$ch" } else { "5G_ch$ch" }
    if (-not $channels.ContainsKey($key)) { $channels[$key] = 0 }
    $channels[$key] += 1
  }
}
[ordered]@{
  tool = "common/probe-wifi-rf-scan"
  bssid_count = ($channels.Values | Measure-Object -Sum).Sum
  channels = $channels
  best_hint = if ($channels.Count -eq 0) { "No BSSID scan data (check location permission or autoconfig)" } else { "Prefer 5GHz channels with lowest BSSID count" }
} | ConvertTo-Json -Depth 4 -Compress
