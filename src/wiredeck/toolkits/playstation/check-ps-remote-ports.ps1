$udpPorts = @(987, 9296, 9297, 9302)
$tcpPorts = @(9295)
$rows = @()
foreach ($u in @(Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $udpPorts })) {
  $pn = (Get-Process -Id $u.OwningProcess -ErrorAction SilentlyContinue).ProcessName
  $rows += [ordered]@{ proto = "UDP"; port = [int]$u.LocalPort; pid = [int]$u.OwningProcess; process = [string]$pn }
}
foreach ($t in @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $tcpPorts })) {
  $pn = (Get-Process -Id $t.OwningProcess -ErrorAction SilentlyContinue).ProcessName
  $rows += [ordered]@{ proto = "TCP"; port = [int]$t.LocalPort; pid = [int]$t.OwningProcess; process = [string]$pn }
}
[ordered]@{ tool = "playstation/check-ps-remote-ports"; udp_ports = $udpPorts; tcp_ports = $tcpPorts; listeners = @($rows) } | ConvertTo-Json -Depth 4 -Compress
