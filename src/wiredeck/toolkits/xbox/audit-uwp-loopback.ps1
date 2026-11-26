$raw = (& CheckNetIsolation.exe LoopbackExempt -s 2>$null) -join "`n"
$exempt = @()
foreach ($m in [regex]::Matches($raw, '(?i)\b([a-z0-9][a-z0-9.-]*_[a-z0-9]{13})\b')) {
  $v = $m.Groups[1].Value.ToLowerInvariant()
  if ($exempt -notcontains $v) { $exempt += $v }
}
$installed = @(Get-AppxPackage -ErrorAction SilentlyContinue | Where-Object {
  $_.PackageFamilyName -match '(?i)^(microsoft\.windowsstore_|microsoft\.gamingapp_|microsoft\.gamingservices_|microsoft\.xbox|microsoft\.aad\.brokerplugin_|microsoft\.accountscontrol_)'
} | ForEach-Object { $_.PackageFamilyName.ToLowerInvariant() })
$missing = @($installed | Where-Object { $exempt -notcontains $_ })
[ordered]@{
  tool = "xbox/audit-uwp-loopback"
  installed_core = @($installed)
  missing_exemptions = @($missing)
  total_exemptions = $exempt.Count
} | ConvertTo-Json -Depth 4 -Compress
