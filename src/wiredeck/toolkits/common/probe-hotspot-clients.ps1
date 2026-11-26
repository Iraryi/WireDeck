$vAdapters = @(Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -match '(?i)wi-?fi direct' } | ForEach-Object {
  [ordered]@{ name = [string]$_.Name; description = [string]$_.InterfaceDescription; status = [string]$_.Status }
})
$clients = @(Get-NetNeighbor -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
  $_.IPAddress -like '192.168.137.*' -and $_.IPAddress -ne '192.168.137.1' -and $_.IPAddress -ne '192.168.137.255'
} | ForEach-Object {
  [ordered]@{ ip = [string]$_.IPAddress; mac = [string]$_.LinkLayerAddress; state = [string]$_.State; interface = [string]$_.InterfaceAlias }
})
[ordered]@{
  tool = "common/probe-hotspot-clients"
  virtual_adapters = $vAdapters
  client_count = $clients.Count
  clients = $clients
} | ConvertTo-Json -Depth 4 -Compress
