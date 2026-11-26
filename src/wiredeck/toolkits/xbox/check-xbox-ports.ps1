$ports = @(3074, 5050, 9002)
$udps = @(Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $ports } | ForEach-Object {
  $pn = (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName
  [ordered]@{ proto = "UDP"; port = [int]$_.LocalPort; pid = [int]$_.OwningProcess; process = [string]$pn }
})
[ordered]@{ tool = "xbox/check-xbox-ports"; ports = $ports; listeners = @($udps) } | ConvertTo-Json -Depth 4 -Compress
