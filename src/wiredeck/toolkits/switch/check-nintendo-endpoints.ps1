$targets = @(
  @{ name = "nintendo_ctest";   url = "http://ctest.cdn.nintendo.net/"; host = "ctest.cdn.nintendo.net" },
  @{ name = "nintendo_conntest"; url = "http://conntest.nintendowifi.net/"; host = "conntest.nintendowifi.net" }
)
$rows = foreach ($t in $targets) {
  $ips = @()
  try { $ips = @((Resolve-DnsName -Name $t.host -Type A -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress }).IPAddress) } catch {}
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $code = 0; $ok = $false
  try {
    $req = [System.Net.HttpWebRequest]::Create($t.url)
    $req.Timeout = 3000
    $req.Method = "GET"
    $resp = $req.GetResponse()
    $code = [int]$resp.StatusCode
    $resp.Close()
    $ok = ($code -ge 200 -and $code -lt 400)
  } catch [System.Net.WebException] {
    if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
  } catch {}
  $sw.Stop()
  [ordered]@{ name = $t.name; host = $t.host; ips = @($ips); http_status = $code; reachable = $ok; latency_ms = $sw.ElapsedMilliseconds }
}
[ordered]@{ tool = "switch/check-nintendo-endpoints"; endpoints = @($rows) } | ConvertTo-Json -Depth 5 -Compress
