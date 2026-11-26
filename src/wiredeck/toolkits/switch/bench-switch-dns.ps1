$servers = @("223.5.5.5", "119.29.29.29", "8.8.8.8", "168.126.63.1")
$domain = "ctest.cdn.nintendo.net"
$rows = foreach ($s in $servers) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $ok = $false; $ip = ""
  try {
    $res = Resolve-DnsName -Name $domain -Server $s -Type A -DnsOnly -ErrorAction Stop | Where-Object { $_.IPAddress } | Select-Object -First 1
    if ($res) { $ok = $true; $ip = [string]$res.IPAddress }
  } catch {}
  $sw.Stop()
  [ordered]@{ dns_server = $s; ok = $ok; resolved_ip = $ip; latency_ms = $sw.ElapsedMilliseconds }
}
[ordered]@{ tool = "switch/bench-switch-dns"; domain = $domain; results = @($rows) } | ConvertTo-Json -Depth 4 -Compress
