"""Read-only Windows Wi-Fi, 5 GHz Mobile Hotspot, and jitter telemetry probes."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from wiredeck.config import AppConfig
from wiredeck.presets import get_preset
from wiredeck.probes import (
    CORE_UWP_FAMILIES,
    _http_probe,
    _ping_peer,
    _windows_oem_encoding,
    detect_local_handheld_clients,
    detect_local_ps_clients,
    detect_local_xstreaming_path,
    parse_uwp_exemptions,
)
from wiredeck.ps_runner import CREATE_NO_WINDOW, PsResult, is_admin, run_powershell
from wiredeck.version import NAT_NAME

# Suboptimal Wi-Fi adapter registry/display values known to trigger periodic stream stutter
WIFI_POWERSAVE_TARGETS: dict[str, dict[str, Any]] = {
    "MIMOPowerSaveMode": {
        "display": "MIMO Power Save Mode",
        "bad_pattern": re.compile(r"(auto|smps|static|dynamic|自动|自動|静|動)", re.IGNORECASE),
        "bad_exclude": re.compile(r"(no\s*smps|无\s*smps|無\s*smps|disabled|关闭|停用)", re.IGNORECASE),
        "optimal_match": re.compile(r"(no\s*smps|无\s*smps|無\s*smps)", re.IGNORECASE),
        "fallback_raw": "0",
    },
    "*PacketCoalescing": {
        "display": "Packet Coalescing",
        "bad_pattern": re.compile(r"(enabled|1|已启用|已啟用)", re.IGNORECASE),
        "bad_exclude": re.compile(r"(disabled|0|已禁用|已停用|关闭)", re.IGNORECASE),
        "optimal_match": re.compile(r"(disabled|已禁用|已停用)", re.IGNORECASE),
        "fallback_raw": "0",
    },
    "PowerSavingMode": {
        "display": "Power Saving Mode",
        "bad_pattern": re.compile(r"(enabled|maximum|1|2|已启用|已啟用)", re.IGNORECASE),
        "bad_exclude": re.compile(r"(disabled|off|0|已禁用|已停用|关闭)", re.IGNORECASE),
        "optimal_match": re.compile(r"(disabled|已禁用|已停用)", re.IGNORECASE),
        "fallback_raw": "0",
    },
    "uAPSDSupport": {
        "display": "U-APSD Support",
        "bad_pattern": re.compile(r"(enabled|1|已启用|已啟用)", re.IGNORECASE),
        "bad_exclude": re.compile(r"(disabled|0|已禁用|已停用)", re.IGNORECASE),
        "optimal_match": re.compile(r"(disabled|已禁用|已停用)", re.IGNORECASE),
        "fallback_raw": "0",
    },
}

_WIRELESS_BATCH_PS = r"""
$result = [ordered]@{
  wifi_adapters = @()
  netsh_interfaces_raw = ""
  netsh_autoconfig_raw = ""
  ipv4_addresses = @()
  wifi_gateway_ip = ""
  default_routes = @()
  net_nats = @()
  hotspot = [ordered]@{
    supported = $false
    band_5ghz_supported = $false
    capability = "Unknown"
    state = "Off"
    band = "Auto"
    ssid = ""
    client_count = 0
    max_client_count = 8
    virtual_adapter_name = ""
    virtual_adapter_ip = ""
  }
  hotspot_clients = @()
  uwp_installed_core = @()
  uwp_exempt_raw = ""
  xbox_app_installed = $false
}

$wifiRe = '(?i)(wi-?fi|wireless|802\.11|wlan|intel.*ax|intel.*ac|mediatek.*wi|realtek.*88|qualcomm)'
$virtRe = '(?i)(vmware|hyper-v|vethernet|wsl|wintun|sing-tun|tailscale|wireguard|openvpn|tap-windows|tunnel|teredo|isatap|loopback|virtualbox|zerotier|lightxtreme|clash|mihomo|bluetooth|miniport|wi-fi direct)'
$tunRe = '(?i)(sing-tun|wintun|clash|mihomo|wireguard|openvpn|lightxtreme|tap-windows|tailscale|nekobox|\btun\b|tunnel)'

$adapters = @()
try { $adapters = @(Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue) } catch {}
$idxMap = @{}
$primaryWifiName = ""

foreach ($a in $adapters) {
  $desc = [string]$a.InterfaceDescription
  $name = [string]$a.Name
  $idxMap[[int]$a.ifIndex] = @{ name = $name; desc = $desc }
  $isDirectVirt = $desc -match '(?i)wi-?fi direct'
  if ($isDirectVirt -and [string]$a.Status -eq 'Up') {
    $result.hotspot.virtual_adapter_name = $name
  }
  $isVirt = ($a.Virtual -eq $true) -or ($desc -match $virtRe) -or ($name -match $virtRe)
  $isWifi = (-not $isVirt) -and (([int]$a.NdisPhysicalMedium -eq 9) -or ($desc -match $wifiRe) -or ($name -match '(?i)^(wi-?fi|wlan)\b'))
  if ($isWifi) {
    if (-not $primaryWifiName -or [string]$a.Status -eq 'Up') { $primaryWifiName = $name }
    $advList = @()
    try {
      $rawAdv = @(Get-NetAdapterAdvancedProperty -Name $name -ErrorAction SilentlyContinue)
      foreach ($p in $rawAdv) {
        $advList += [ordered]@{
          keyword = [string]$p.RegistryKeyword
          display_name = [string]$p.DisplayName
          display_value = [string]$p.DisplayValue
          registry_value = ($p.RegistryValue -join ',')
        }
      }
    } catch {}
    $bps = 0
    try { $bps = [int64]$a.Speed } catch {}
    $result.wifi_adapters += [ordered]@{
      name = $name
      interface_description = $desc
      interface_index = [int]$a.ifIndex
      status = [string]$a.Status
      link_speed_bps = $bps
      link_speed_text = [string]$a.LinkSpeed
      pnp_device_id = [string]$a.PnPDeviceID
      advanced_properties = $advList
    }
  }
}

try { $result.netsh_interfaces_raw = ((& netsh wlan show interfaces 2>$null) -join "`n") } catch {}
try { $result.netsh_autoconfig_raw = ((& netsh wlan show autoconfig 2>$null) -join "`n") } catch {}

try {
  $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' })
  foreach ($ip in $ips) {
    $result.ipv4_addresses += [ordered]@{
      interface_alias = [string]$ip.InterfaceAlias
      interface_index = [int]$ip.InterfaceIndex
      ip = [string]$ip.IPAddress
      prefix_length = [int]$ip.PrefixLength
    }
    if ($ip.IPAddress -eq '192.168.137.1' -or ($result.hotspot.virtual_adapter_name -and $ip.InterfaceAlias -eq $result.hotspot.virtual_adapter_name)) {
      $result.hotspot.virtual_adapter_ip = [string]$ip.IPAddress
    }
  }
} catch {}

try {
  $routes = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
  foreach ($r in $routes) {
    $alias = [string]$r.InterfaceAlias
    $desc = if ($idxMap.ContainsKey([int]$r.InterfaceIndex)) { [string]$idxMap[[int]$r.InterfaceIndex].desc } else { "" }
    $isTun = ($alias -match $tunRe) -or ($desc -match $tunRe)
    $result.default_routes += [ordered]@{
      interface_alias = $alias
      interface_index = [int]$r.InterfaceIndex
      next_hop = [string]$r.NextHop
      metric = [int]$r.RouteMetric
      is_virtual_or_tun = [bool]$isTun
      interface_description = $desc
    }
    if (-not $isTun -and $primaryWifiName -and $alias -eq $primaryWifiName -and -not $result.wifi_gateway_ip) {
      $result.wifi_gateway_ip = [string]$r.NextHop
    }
  }
} catch {}

try {
  $nats = @(Get-NetNat -ErrorAction SilentlyContinue)
  foreach ($n in $nats) {
    $result.net_nats += [ordered]@{ name = [string]$n.Name; internal_subnet = [string]$n.InternalIPInterfaceAddressPrefix }
  }
} catch {}

try {
  [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime] | Out-Null
  [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null
  $prof = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
  if (-not $prof) {
    $profiles = [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()
    if ($profiles -and $profiles.Count -gt 0) { $prof = $profiles[0] }
  }
  if ($prof) {
    $cap = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::GetTetheringCapabilityFromConnectionProfile($prof)
    $result.hotspot.capability = [string]$cap
    $tm = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($prof)
    $ap = $tm.GetCurrentAccessPointConfiguration()
    $supports5g = $false
    try { $supports5g = [bool]$ap.IsBandSupported([Windows.Networking.NetworkOperators.TetheringWiFiBand]::FiveGigahertz) } catch {}
    $result.hotspot.supported = ([string]$cap -eq 'Enabled') -or ([string]$tm.TetheringOperationalState -eq 'On')
    $result.hotspot.band_5ghz_supported = $supports5g
    $result.hotspot.state = [string]$tm.TetheringOperationalState
    $result.hotspot.band = [string]$ap.Band
    # Security: NEVER include $ap.Passphrase in snapshot!
    $result.hotspot.ssid = [string]$ap.Ssid
    $result.hotspot.client_count = [int]$tm.ClientCount
    $result.hotspot.max_client_count = [int]$tm.MaxClientCount
  }
} catch {}

try {
  $neighbors = @(Get-NetNeighbor -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
    $_.IPAddress -like '192.168.137.*' -and
    $_.IPAddress -ne '192.168.137.1' -and
    $_.IPAddress -ne '192.168.137.255' -and
    $_.State -in @('Reachable','Stale','Delay','Probe')
  })
  foreach ($nb in $neighbors) {
    $result.hotspot_clients += [ordered]@{
      ip = [string]$nb.IPAddress
      mac = [string]$nb.LinkLayerAddress
      state = [string]$nb.State
      interface_alias = [string]$nb.InterfaceAlias
    }
  }
} catch {}

if ('__NEED_UWP__' -eq '1') {
  try {
    $coreSet = @(__CORE_LIST__)
    $pkgs = @(Get-AppxPackage -ErrorAction SilentlyContinue)
    foreach ($pk in $pkgs) {
      $pfn = [string]$pk.PackageFamilyName
      if ($pfn.ToLower() -eq 'microsoft.gamingapp_8wekyb3d8bbwe') { $result.xbox_app_installed = $true }
      if ($coreSet -contains $pfn.ToLower()) { $result.uwp_installed_core += $pfn.ToLower() }
    }
  } catch {}
  try { $result.uwp_exempt_raw = ((& CheckNetIsolation.exe LoopbackExempt -s 2>$null) -join "`n") } catch {}
}

$result | ConvertTo-Json -Depth 6 -Compress
"""


def parse_netsh_wlan_interfaces(text: str) -> dict[str, Any]:
    """Parse Windows `netsh wlan show interfaces` output across EN / zh-CN / zh-TW locales."""
    out: dict[str, Any] = {
        "name": "",
        "description": "",
        "state": "disconnected",
        "connected": False,
        "ssid": "",
        "band": "",
        "band_ghz": None,
        "channel": None,
        "radio_type": "",
        "rx_rate_mbps": None,
        "tx_rate_mbps": None,
        "signal_pct": None,
        "rssi_dbm": None,
    }
    if not text.strip():
        return out

    key_map: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r"^(?:Name|名称|名稱)$", re.IGNORECASE), "name"),
        (re.compile(r"^(?:Description|描述)$", re.IGNORECASE), "description"),
        (re.compile(r"^(?:State|状态|狀態)$", re.IGNORECASE), "state_raw"),
        (re.compile(r"^SSID$", re.IGNORECASE), "ssid"),
        (re.compile(r"^(?:Band|波段|頻帶)$", re.IGNORECASE), "band"),
        (re.compile(r"^(?:Channel|频道|頻道)$", re.IGNORECASE), "channel"),
        (re.compile(r"^(?:Radio type|无线电类型|無線電類型)$", re.IGNORECASE), "radio_type"),
        (re.compile(r"^(?:Receive rate\s*\(Mbps\)|接收速率\s*\(Mbps\))$", re.IGNORECASE), "rx"),
        (re.compile(r"^(?:Transmit rate\s*\(Mbps\)|传输速率\s*\(Mbps\)|傳送速率\s*\(Mbps\))$", re.IGNORECASE), "tx"),
        (re.compile(r"^(?:Signal|信号|訊號)$", re.IGNORECASE), "signal"),
        (re.compile(r"^Rssi$", re.IGNORECASE), "rssi"),
    ]

    for raw_line in text.splitlines():
        if ":" not in raw_line:
            continue
        k, v = raw_line.split(":", 1)
        k = k.strip()
        v = v.strip()
        field = None
        for rx, target in key_map:
            if rx.match(k):
                field = target
                break
        if not field:
            continue
        if field == "name" and not out["name"]:
            out["name"] = v
        elif field == "description" and not out["description"]:
            out["description"] = v
        elif field == "state_raw":
            low = v.lower()
            is_conn = ("connected" in low and "disconnected" not in low) or ("已连接" in v) or ("已連線" in v)
            out["connected"] = is_conn
            out["state"] = "connected" if is_conn else "disconnected"
        elif field == "ssid" and not out["ssid"]:
            out["ssid"] = v
        elif field == "band":
            out["band"] = v
            if "6" in v:
                out["band_ghz"] = 6.0
            elif "5" in v:
                out["band_ghz"] = 5.0
            elif "2.4" in v:
                out["band_ghz"] = 2.4
        elif field == "channel":
            m = re.search(r"(\d+)", v)
            if m:
                ch = int(m.group(1))
                out["channel"] = ch
                if out["band_ghz"] is None:
                    out["band_ghz"] = 5.0 if ch > 14 else 2.4
        elif field == "radio_type":
            out["radio_type"] = v
        elif field == "rx":
            m = re.search(r"(\d+(?:\.\d+)?)", v)
            if m:
                out["rx_rate_mbps"] = float(m.group(1))
        elif field == "tx":
            m = re.search(r"(\d+(?:\.\d+)?)", v)
            if m:
                out["tx_rate_mbps"] = float(m.group(1))
        elif field == "signal":
            m = re.search(r"(\d+)", v)
            if m:
                out["signal_pct"] = int(m.group(1))
        elif field == "rssi":
            m = re.search(r"(-?\d+)", v)
            if m:
                out["rssi_dbm"] = int(m.group(1))

    # Infer band if channel present or radio type is 802.11a/ac without band header (Windows 10)
    if out["band_ghz"] is None and out["channel"] is not None:
        out["band_ghz"] = 5.0 if int(out["channel"]) > 14 else 2.4
    if not out["band"] and out["band_ghz"] is not None:
        out["band"] = "5 GHz" if out["band_ghz"] == 5.0 else ("6 GHz" if out["band_ghz"] == 6.0 else "2.4 GHz")
    return out


def parse_netsh_autoconfig(text: str, iface_name: str = "") -> bool | None:
    """Return True if WLAN AutoConfig periodic scanning is enabled, False if disabled, None if unknown."""
    if not text.strip():
        return None
    for line in text.splitlines():
        if iface_name and iface_name.lower() not in line.lower():
            continue
        low = line.lower()
        if "disabled" in low or "已禁用" in line or "已停用" in line:
            return False
        if "enabled" in low or "已启用" in line or "已啟用" in line:
            return True
    # Fallback scan across any line if interface name wasn't matched
    if iface_name:
        return parse_netsh_autoconfig(text, iface_name="")
    return None


def parse_ping_series(out: str) -> list[float]:
    """Extract per-packet RTT milliseconds from Windows ping output across EN/CN locales."""
    samples: list[float] = []
    for m in re.finditer(r"(?:time|时间|時間)\s*([<=])\s*(\d+(?:\.\d+)?)\s*ms", out, re.IGNORECASE):
        val = float(m.group(2))
        samples.append(0.5 if (m.group(1) == "<" and val <= 1.0) else val)
    return samples


def measure_wifi_jitter(target_ip: str, count: int = 6, timeout_ms: int = 750) -> dict[str, Any]:
    """Run bounded ICMP echo series to measure Wi-Fi RTT min/avg/max, jitter, and packet loss."""
    empty = {
        "target_ip": target_ip,
        "reachable": False,
        "sent": count,
        "received": 0,
        "loss_pct": 100.0,
        "min_rtt_ms": None,
        "avg_rtt_ms": None,
        "max_rtt_ms": None,
        "jitter_ms": None,
    }
    try:
        ipaddress.IPv4Address(target_ip)
    except ValueError:
        return empty

    creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            ["ping", "-n", str(count), "-w", str(timeout_ms), target_ip],
            capture_output=True,
            timeout=max(4.0, count * (timeout_ms / 1000.0) + 2.0),
            creationflags=creationflags,
        )
        raw = proc.stdout or b""
        enc = _windows_oem_encoding()
        text = raw.decode(enc, errors="replace")
        samples = parse_ping_series(text)
        if not samples and enc.lower() != "utf-8":
            samples = parse_ping_series(raw.decode("utf-8", errors="replace"))
        if not samples:
            return empty
        diffs = [abs(samples[i] - samples[i - 1]) for i in range(1, len(samples))]
        jitter = round(statistics.mean(diffs), 2) if diffs else 0.0
        recv = len(samples)
        loss_pct = round(max(0.0, (count - recv) / float(count) * 100.0), 1)
        return {
            "target_ip": target_ip,
            "reachable": True,
            "sent": count,
            "received": recv,
            "loss_pct": loss_pct,
            "min_rtt_ms": round(min(samples), 2),
            "avg_rtt_ms": round(statistics.mean(samples), 2),
            "max_rtt_ms": round(max(samples), 2),
            "jitter_ms": jitter,
        }
    except Exception:
        return empty


def detect_suboptimal_wifi_props(adv_props: list[dict[str, Any]]) -> list[str]:
    """Return registry keywords whose current Wi-Fi value degrades streaming latency."""
    flagged: list[str] = []
    for prop in adv_props:
        kw = str(prop.get("keyword", ""))
        spec = WIFI_POWERSAVE_TARGETS.get(kw)
        if not spec:
            continue
        disp = f"{prop.get('display_value', '')} {prop.get('registry_value', '')}".strip()
        if spec["bad_exclude"].search(disp):
            continue
        if spec["bad_pattern"].search(disp):
            flagged.append(kw)
    return flagged


def probe_wireless_connectivity(
    cfg: AppConfig,
    target_peer_ip: str = "",
    gateway_ip: str = "",
) -> dict[str, Any]:
    """Run concurrent Wi-Fi peer jitter ping, gateway reachability, internet, and preset service probes."""
    preset = get_preset(cfg.active_preset)
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_peer = pool.submit(measure_wifi_jitter, target_peer_ip, 5, 650) if target_peer_ip else None
        f_gw = (
            pool.submit(_ping_peer, gateway_ip, 500)
            if (gateway_ip and cfg.wireless_submode == "lan" and target_peer_ip)
            else None
        )
        f_inet = pool.submit(_http_probe, "http://www.msftconnecttest.com/connecttest.txt", 2.0)
        f_svc = (
            pool.submit(_http_probe, preset.service_endpoint_url, 2.5)
            if preset.service_endpoint_url
            else None
        )

        jitter = f_peer.result() if f_peer is not None else None
        gw_ok, _ = f_gw.result() if f_gw is not None else (None, None)
        inet_ok, _ = f_inet.result()
        if not inet_ok:
            inet_ok, _ = _http_probe("https://login.microsoftonline.com/", 2.0)
        svc_ok, svc_status = f_svc.result() if f_svc is not None else (None, None)

    return {
        "target_peer_ip": target_peer_ip,
        "peer_reachable": jitter["reachable"] if jitter else None,
        "peer_rtt_ms": jitter["avg_rtt_ms"] if jitter else None,
        "jitter_ms": jitter["jitter_ms"] if jitter else None,
        "loss_pct": jitter["loss_pct"] if jitter else None,
        "gateway_ip": gateway_ip,
        "gateway_reachable": gw_ok,
        "host_internet_ok": inet_ok,
        "xbox_live_ok": svc_ok if preset.id == "xbox" else None,
        "service_endpoint_ok": svc_ok,
        "service_endpoint_status": svc_status,
    }


def collect_wireless_snapshot(
    cfg: AppConfig,
    runner: Callable[[str, int], PsResult] = run_powershell,
    *,
    probe_network: bool = True,
) -> dict[str, Any]:
    """Collect a read-only Windows Wi-Fi, 5GHz Mobile Hotspot, and jitter snapshot."""
    preset = get_preset(cfg.active_preset)
    core_list = ", ".join(f"'{p}'" for p in CORE_UWP_FAMILIES)
    ps_script = _WIRELESS_BATCH_PS.replace("__NEED_UWP__", "1" if preset.uses_uwp_loopback else "0").replace(
        "__CORE_LIST__", core_list
    )

    ps_res = runner(ps_script, 15)
    parsed: dict[str, Any] = {}
    if ps_res.stdout.strip():
        text = ps_res.stdout.strip()
        first_brace = text.find("{")
        if first_brace >= 0:
            try:
                parsed = json.loads(text[first_brace:])
            except json.JSONDecodeError:
                parsed = {"probe_parse_error": text[:300]}
    if not isinstance(parsed, dict):
        parsed = {}

    wifi_adapters = parsed.get("wifi_adapters") or []
    for wa in wifi_adapters:
        wa["powersave_enabled"] = detect_suboptimal_wifi_props(wa.get("advanced_properties") or [])

    wlan_info = parse_netsh_wlan_interfaces(str(parsed.get("netsh_interfaces_raw", "")))
    iface_name = cfg.wifi_interface or wlan_info.get("name") or (wifi_adapters[0]["name"] if wifi_adapters else "")
    autoconfig_enabled = parse_netsh_autoconfig(str(parsed.get("netsh_autoconfig_raw", "")), iface_name)

    hotspot = parsed.get("hotspot") or {}
    clients = parsed.get("hotspot_clients") or []

    # Determine target peer IP cleanly based on wireless submode
    target_peer_ip = ""
    if cfg.wireless_submode == "hotspot":
        if clients:
            target_peer_ip = str(clients[0].get("ip", ""))
    else:
        target_peer_ip = cfg.wireless_peer_ip.strip()

    gateway_ip = str(parsed.get("wifi_gateway_ip") or "")

    if probe_network:
        connectivity = probe_wireless_connectivity(cfg, target_peer_ip=target_peer_ip, gateway_ip=gateway_ip)
    else:
        connectivity = {
            "target_peer_ip": target_peer_ip,
            "peer_reachable": None,
            "peer_rtt_ms": None,
            "jitter_ms": None,
            "loss_pct": None,
            "gateway_ip": gateway_ip,
            "gateway_reachable": None,
            "host_internet_ok": None,
            "xbox_live_ok": None,
            "service_endpoint_ok": None,
            "service_endpoint_status": None,
        }

    exempt_packages = parse_uwp_exemptions(str(parsed.get("uwp_exempt_raw", "")))
    installed_core = [str(p).lower() for p in (parsed.get("uwp_installed_core") or [])]
    target_core = [p for p in CORE_UWP_FAMILIES if p in installed_core] if installed_core else []
    missing_uwp = [p for p in target_core if p not in exempt_packages]

    wired_nat_active = any(str(n.get("name", "")) == NAT_NAME for n in (parsed.get("net_nats") or []))

    return {
        "is_admin": is_admin(),
        "probe_error": ps_res.stderr.strip() if ps_res.returncode != 0 else "",
        "wifi_adapters": wifi_adapters,
        "wlan": wlan_info,
        "autoconfig_enabled": autoconfig_enabled,
        "ipv4_addresses": parsed.get("ipv4_addresses") or [],
        "wifi_gateway_ip": gateway_ip,
        "default_routes": parsed.get("default_routes") or [],
        "wired_nat_active": wired_nat_active,
        "hotspot": hotspot,
        "hotspot_clients": clients,
        "uwp_exempt_packages": exempt_packages,
        "uwp_missing_packages": missing_uwp,
        "stream_clients": {
            "xstreaming_path": detect_local_xstreaming_path(),
            "xbox_app_installed": bool(parsed.get("xbox_app_installed", False)),
            **detect_local_ps_clients(),
            **detect_local_handheld_clients(),
        },
        "connectivity": connectivity,
    }


def get_hotspot_credentials(
    runner: Callable[[str, int], PsResult] = run_powershell,
) -> dict[str, Any]:
    """Fetch Mobile Hotspot SSID & passphrase for in-app GUI copy ONLY (never logged or exported)."""
    ps = r"""
try {
  [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime] | Out-Null
  [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null
  $prof = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
  if (-not $prof) {
    $profiles = [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()
    if ($profiles -and $profiles.Count -gt 0) { $prof = $profiles[0] }
  }
  if (-not $prof) {
    Write-Output '{"ok":false,"error":"NO_PROFILE"}'
    exit 0
  }
  $tm = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($prof)
  $ap = $tm.GetCurrentAccessPointConfiguration()
  [ordered]@{
    ok = $true
    ssid = [string]$ap.Ssid
    passphrase = [string]$ap.Passphrase
    band = [string]$ap.Band
    state = [string]$tm.TetheringOperationalState
    client_count = [int]$tm.ClientCount
  } | ConvertTo-Json -Compress
} catch {
  Write-Output ('{"ok":false,"error":"' + ($_.Exception.Message -replace '"', "'") + '"}')
}
"""
    res = runner(ps, 10)
    txt = res.stdout.strip()
    if "{" in txt:
        try:
            data = json.loads(txt[txt.find("{") :])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"ok": False, "error": res.stderr.strip() or "CREDENTIAL_READ_FAILED"}
