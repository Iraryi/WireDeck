$hostIp = if ($env:WIREDECK_HOST_IP) { $env:WIREDECK_HOST_IP } else { "192.168.50.2" }
$peerIp = if ($env:WIREDECK_PEER_IP) { $env:WIREDECK_PEER_IP } else { "192.168.50.1" }
$mask = if ($env:WIREDECK_SUBNET_MASK) { $env:WIREDECK_SUBNET_MASK } else { "255.255.255.0" }
$dns = if ($env:WIREDECK_DNS_SERVERS) { ($env:WIREDECK_DNS_SERVERS -split ',') } else { @("223.5.5.5", "8.8.8.8") }
$dnsPrimary = $dns[0]
[ordered]@{
  tool = "other/emit-peer-config-snippets"
  host_ip = $hostIp
  peer_ip = $peerIp
  subnet_mask = $mask
  dns = @($dns)
  linux = "sudo ip addr replace $peerIp/24 dev eth0 && sudo ip route replace default via $hostIp dev eth0"
  macos = "sudo networksetup -setmanual `"USB 10/100/1000 LAN`" $peerIp $mask $hostIp && sudo networksetup -setdnsservers `"USB 10/100/1000 LAN`" $($dns -join ' ')"
  windows = "New-NetIPAddress -InterfaceAlias 'Ethernet' -IPAddress $peerIp -PrefixLength 24 -DefaultGateway $hostIp; Set-DnsClientServerAddress -InterfaceAlias 'Ethernet' -ServerAddresses '$dnsPrimary'"
} | ConvertTo-Json -Depth 4 -Compress
