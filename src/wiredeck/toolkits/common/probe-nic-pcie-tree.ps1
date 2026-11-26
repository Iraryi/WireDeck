$virtualRe = '(?i)(vmware|hyper-v|vethernet|wsl|wintun|sing-tun|tailscale|wireguard|openvpn|tap-windows|tunnel|bluetooth|wi-?fi|wireless|802\.11|wlan)'
$adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { (-not $_.Virtual) -and ($_.InterfaceDescription -notmatch $virtualRe) })
$items = foreach ($a in $adapters) {
  $ps = @(Get-NetAdapterAdvancedProperty -Name $a.Name -ErrorAction SilentlyContinue | Where-Object { $_.RegistryKeyword -in @('*EEE','PowerSavingMode','EnableGreenEthernet','GigaLite','EEELinkAdvertisement','AutoDisableGigabit') } | ForEach-Object {
    [ordered]@{ key = [string]$_.RegistryKeyword; value = ($_.RegistryValue -join ','); display = [string]$_.DisplayValue }
  })
  [ordered]@{
    name = [string]$a.Name
    description = [string]$a.InterfaceDescription
    status = [string]$a.Status
    link_speed = [string]$a.LinkSpeed
    driver_version = [string]$a.DriverVersion
    pnp_id = [string]$a.PnPDeviceID
    powersave_props = @($ps)
  }
}
[ordered]@{
  tool = "common/probe-nic-pcie-tree"
  wired_nics = @($items)
} | ConvertTo-Json -Depth 5 -Compress
