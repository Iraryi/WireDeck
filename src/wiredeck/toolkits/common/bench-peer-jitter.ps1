$peer = if ($env:WIREDECK_PEER_IP) { $env:WIREDECK_PEER_IP } else { "192.168.50.1" }
$rtts = @()
$sent = 8
for ($i = 0; $i -lt $sent; $i++) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $ok = Test-Connection -ComputerName $peer -Count 1 -Quiet -ErrorAction SilentlyContinue
  $sw.Stop()
  if ($ok) { $rtts += [math]::Round($sw.Elapsed.TotalMilliseconds, 2) }
}
$recv = $rtts.Count
$lossPct = [math]::Round((($sent - $recv) / $sent) * 100, 1)
$min = if ($recv -gt 0) { ($rtts | Measure-Object -Minimum).Minimum } else { $null }
$max = if ($recv -gt 0) { ($rtts | Measure-Object -Maximum).Maximum } else { $null }
$avg = if ($recv -gt 0) { [math]::Round(($rtts | Measure-Object -Average).Average, 2) } else { $null }
[ordered]@{
  tool = "common/bench-peer-jitter"
  peer_ip = $peer
  sent = $sent
  received = $recv
  loss_pct = $lossPct
  min_ms = $min
  avg_ms = $avg
  max_ms = $max
} | ConvertTo-Json -Depth 4 -Compress
