$tcpPorts = @(47984, 47989, 48010)
$udpPorts = @(47998, 47999, 48000, 27031, 27036)
$rows = @()
foreach ($t in @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $tcpPorts })) {
  $pn = (Get-Process -Id $t.OwningProcess -ErrorAction SilentlyContinue).ProcessName
  $rows += [ordered]@{ proto = "TCP"; port = [int]$t.LocalPort; pid = [int]$t.OwningProcess; process = [string]$pn }
}
foreach ($u in @(Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $udpPorts })) {
  $pn = (Get-Process -Id $u.OwningProcess -ErrorAction SilentlyContinue).ProcessName
  $rows += [ordered]@{ proto = "UDP"; port = [int]$u.LocalPort; pid = [int]$u.OwningProcess; process = [string]$pn }
}
[ordered]@{ tool = "handheld/check-stream-host-ports"; tcp_ports = $tcpPorts; udp_ports = $udpPorts; listeners = @($rows) } | ConvertTo-Json -Depth 4 -Compress
