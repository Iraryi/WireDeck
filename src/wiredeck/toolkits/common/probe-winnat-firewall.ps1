$nats = @(Get-NetNat -ErrorAction SilentlyContinue | ForEach-Object {
  [ordered]@{ name = [string]$_.Name; internal_prefix = [string]$_.InternalIPInterfaceAddressPrefix; active = [bool]$_.Active }
})
$fwds = @(Get-NetIPInterface -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.Forwarding -eq 'Enabled' } | ForEach-Object {
  [ordered]@{ interface = [string]$_.InterfaceAlias; if_index = [int]$_.ifIndex }
})
$fwRules = @()
try {
  $fw = New-Object -ComObject HNetCfg.FwPolicy2
  foreach ($n in @('WireDeck-WinNAT-Allow', 'WireDeck-Direct-Allow')) {
    $exists = $false
    try { $null = $fw.Rules.Item($n); $exists = $true } catch {}
    $fwRules += [ordered]@{ name = $n; exists = $exists }
  }
} catch {}
[ordered]@{
  tool = "common/probe-winnat-firewall"
  net_nats = @($nats)
  forwarding_enabled = @($fwds)
  firewall_rules = @($fwRules)
} | ConvertTo-Json -Depth 5 -Compress
