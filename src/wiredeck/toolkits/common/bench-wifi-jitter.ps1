$target = if ($env:WIREDECK_WIRELESS_PEER_IP) { $env:WIREDECK_WIRELESS_PEER_IP } else { '' }
if (-not $target) {
  $nb = Get-NetNeighbor -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like '192.168.137.*' -and $_.IPAddress -ne '192.168.137.1' -and $_.State -in @('Reachable','Stale','Delay') } | Select-Object -First 1
  if ($nb) { $target = [string]$nb.IPAddress }
}
if (-not $target) {
  $gw = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceAlias -match '(?i)wi-?fi|wlan' } | Sort-Object RouteMetric | Select-Object -First 1
  if ($gw) { $target = [string]$gw.NextHop }
}
if (-not $target) {
  [ordered]@{ tool = "common/bench-wifi-jitter"; error = "NO_TARGET_IP" } | ConvertTo-Json -Compress
  exit 0
}
$rtts = @()
$pingOut = & ping.exe -n 20 -w 600 $target 2>$null
foreach ($ln in $pingOut) {
  if ($ln -match '(?:time|时间|時間)\s*([<=])\s*(\d+(?:\.\d+)?)\s*ms') {
    $v = [double]$matches[2]
    if ($matches[1] -eq '<' -and $v -le 1) { $v = 0.5 }
    $rtts += $v
  }
}
$recv = $rtts.Count
$loss = [math]::Round((20 - $recv) / 20.0 * 100.0, 1)
if ($recv -eq 0) {
  [ordered]@{ tool = "common/bench-wifi-jitter"; target = $target; sent = 20; received = 0; loss_pct = 100.0 } | ConvertTo-Json -Compress
  exit 0
}
$sorted = @($rtts | Sort-Object)
$p95Idx = [math]::Min($recv - 1, [int][math]::Floor(0.95 * $recv))
$diffs = @()
for ($i = 1; $i -lt $recv; $i++) { $diffs += [math]::Abs($rtts[$i] - $rtts[$i-1]) }
$jitter = if ($diffs.Count -gt 0) { [math]::Round(($diffs | Measure-Object -Average).Average, 2) } else { 0.0 }
[ordered]@{
  tool = "common/bench-wifi-jitter"
  target = $target
  sent = 20
  received = $recv
  loss_pct = $loss
  min_ms = [math]::Round($sorted[0], 2)
  avg_ms = [math]::Round(($rtts | Measure-Object -Average).Average, 2)
  p95_ms = [math]::Round($sorted[$p95Idx], 2)
  max_ms = [math]::Round($sorted[$recv - 1], 2)
  jitter_ms = $jitter
} | ConvertTo-Json -Depth 4 -Compress
