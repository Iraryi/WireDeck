$wifi = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { ($_.Virtual -ne $true) -and (([int]$_.NdisPhysicalMedium -eq 9) -or ($_.InterfaceDescription -match '(?i)wi-?fi|wireless|802\.11|wlan')) } | Select-Object -First 1
if (-not $wifi) {
  [ordered]@{ tool = "common/probe-wifi-advanced"; error = "WIFI_NOT_FOUND" } | ConvertTo-Json -Compress
  exit 0
}
$adv = @(Get-NetAdapterAdvancedProperty -Name $wifi.Name -ErrorAction SilentlyContinue | Select-Object RegistryKeyword, DisplayName, DisplayValue, @{N='RegistryValue';E={$_.RegistryValue -join ','}})
$autoconfig = (& netsh wlan show autoconfig 2>$null) -join ' '
[ordered]@{
  tool = "common/probe-wifi-advanced"
  adapter = [string]$wifi.Name
  description = [string]$wifi.InterfaceDescription
  status = [string]$wifi.Status
  link_speed = [string]$wifi.LinkSpeed
  driver_version = [string]$wifi.DriverVersion
  autoconfig_raw = $autoconfig.Trim()
  advanced_properties = $adv
} | ConvertTo-Json -Depth 4 -Compress
