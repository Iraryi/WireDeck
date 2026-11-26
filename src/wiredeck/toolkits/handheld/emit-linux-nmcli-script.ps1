$hostIp = if ($env:WIREDECK_HOST_IP) { $env:WIREDECK_HOST_IP } else { "192.168.50.2" }
$peerIp = if ($env:WIREDECK_PEER_IP) { $env:WIREDECK_PEER_IP } else { "192.168.50.30" }
$subnet = if ($env:WIREDECK_SUBNET_CIDR) { $env:WIREDECK_SUBNET_CIDR } else { "192.168.50.0/24" }
$dns = if ($env:WIREDECK_DNS_SERVERS) { ($env:WIREDECK_DNS_SERVERS -replace ',', ' ') } else { "223.5.5.5 8.8.8.8" }
[ordered]@{
  tool = "handheld/emit-linux-nmcli-script"
  host_ip = $hostIp
  peer_ip = $peerIp
  subnet = $subnet
  nmcli_setup = "sudo nmcli con add type ethernet ifname eth0 con-name wiredeck-direct ipv4.method manual ipv4.addresses $peerIp/24 ipv4.gateway $hostIp ipv4.dns `"$dns`" && sudo nmcli con up wiredeck-direct"
  policy_route_temp = "sudo ip route replace $subnet dev eth0 src $peerIp table 50 && sudo ip route replace default via $hostIp dev eth0 src $peerIp table 50 && sudo ip rule add from $peerIp/32 lookup 50 priority 50"
} | ConvertTo-Json -Depth 4 -Compress
