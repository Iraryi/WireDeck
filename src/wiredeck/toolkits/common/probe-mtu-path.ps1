$target = "223.5.5.5"
$payloadSizes = @(1472, 1464, 1452, 1420, 1372)
$results = @()
$bestMtu = 0
foreach ($sz in $payloadSizes) {
  $p = & ping.exe -n 1 -f -l $sz -w 900 $target 2>$null
  $ok = ($LASTEXITCODE -eq 0)
  $mtu = $sz + 28
  $results += [ordered]@{ payload = $sz; mtu = $mtu; ok = $ok }
  if ($ok -and $bestMtu -eq 0) { $bestMtu = $mtu }
}
[ordered]@{
  tool = "common/probe-mtu-path"
  target = $target
  recommended_max_mtu = $bestMtu
  probes = @($results)
} | ConvertTo-Json -Depth 5 -Compress
