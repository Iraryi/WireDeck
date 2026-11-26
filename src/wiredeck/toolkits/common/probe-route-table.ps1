$routes = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Sort-Object RouteMetric)
$out = foreach ($r in $routes) {
  $a = Get-NetAdapter -InterfaceIndex $r.ifIndex -ErrorAction SilentlyContinue
  [ordered]@{
    interface_alias = [string]$r.InterfaceAlias
    if_index = [int]$r.ifIndex
    next_hop = [string]$r.NextHop
    route_metric = [int]$r.RouteMetric
    description = if ($a) { [string]$a.InterfaceDescription } else { "" }
    status = if ($a) { [string]$a.Status } else { "Unknown" }
    virtual = if ($a) { [bool]$a.Virtual } else { $false }
  }
}
[ordered]@{
  tool = "common/probe-route-table"
  default_route_count = @($out).Count
  default_routes = @($out)
} | ConvertTo-Json -Depth 5 -Compress
