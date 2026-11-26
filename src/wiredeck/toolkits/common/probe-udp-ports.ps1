$ports = @(53, 67, 3074, 5050, 9295, 9296, 9297, 9302, 47984, 47998, 47999, 48000)
$udps = @(Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $ports })
$tcps = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $ports })
$rows = @()
foreach ($u in $udps) {
  $pn = (Get-Process -Id $u.OwningProcess -ErrorAction SilentlyContinue).ProcessName
  $rows += [ordered]@{ proto = "UDP"; port = [int]$u.LocalPort; pid = [int]$u.OwningProcess; process = [string]$pn; address = [string]$u.LocalAddress }
}
foreach ($t in $tcps) {
  $pn = (Get-Process -Id $t.OwningProcess -ErrorAction SilentlyContinue).ProcessName
  $rows += [ordered]@{ proto = "TCP"; port = [int]$t.LocalPort; pid = [int]$t.OwningProcess; process = [string]$pn; address = [string]$t.LocalAddress }
}
[ordered]@{
  tool = "common/probe-udp-ports"
  checked_ports = $ports
  listeners = @($rows)
} | ConvertTo-Json -Depth 5 -Compress
