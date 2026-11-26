$targets = @(
  @{ name = "psn_auth";    url = "https://auth.api.sonyentertainmentnetwork.com/"; host = "auth.api.sonyentertainmentnetwork.com" },
  @{ name = "sony_acct";   url = "https://ca.account.sony.com/"; host = "ca.account.sony.com" },
  @{ name = "ps_store";    url = "https://store.playstation.com/"; host = "store.playstation.com" }
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
    $ok = $true
  } catch [System.Net.WebException] {
    if ($_.Exception.Response) {
      $code = [int]$_.Exception.Response.StatusCode
      $ok = ($code -ge 200 -and $code -lt 500)
    }
  } catch {}
  $sw.Stop()
  [ordered]@{ name = $t.name; host = $t.host; ips = @($ips); http_status = $code; reachable = $ok; latency_ms = $sw.ElapsedMilliseconds }
}
[ordered]@{ tool = "playstation/check-psn-endpoints"; endpoints = @($rows) } | ConvertTo-Json -Depth 5 -Compress
