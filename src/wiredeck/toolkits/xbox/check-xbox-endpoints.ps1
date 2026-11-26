$targets = @(
  @{ name = "xbox_auth"; url = "https://user.auth.xboxlive.com/user/authenticate"; host = "user.auth.xboxlive.com" },
  @{ name = "xsts_auth"; url = "https://xsts.auth.xboxlive.com/xsts/authorize"; host = "xsts.auth.xboxlive.com" },
  @{ name = "store_api"; url = "https://displaycatalog.mp.microsoft.com/v7.0/products?bigIds=9NBLGGH537C2&market=US&languages=en-us"; host = "displaycatalog.mp.microsoft.com" },
  @{ name = "ms_login";  url = "https://login.microsoftonline.com/"; host = "login.microsoftonline.com" }
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
[ordered]@{ tool = "xbox/check-xbox-endpoints"; endpoints = @($rows) } | ConvertTo-Json -Depth 5 -Compress
