"""Dynamic read-only Windows system probes for WireDeck 6-layer diagnosis."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from wiredeck.config import AppConfig
from wiredeck.presets import get_preset
from wiredeck.ps_runner import CREATE_NO_WINDOW, PsResult, is_admin, run_powershell
from wiredeck.version import APP_VERSION, FW_RULE_DIRECT, FW_RULE_NAT

VIRTUAL_DESC_RE = re.compile(
    r"(vmware|hyper-v|vethernet|wsl|wintun|sing-tun|tailscale|wireguard|"
    r"openvpn|tap-windows|tunnel|teredo|isatap|6to4|loopback|virtualbox|"
    r"zero\s*tier|radmin|hamachi|lightxtreme|clash|mihomo|nekobox|bluetooth|"
    r"miniport|wan\s*miniport|microsoft\s*wi-fi\s*direct|kernel\s*debug)",
    re.IGNORECASE,
)

WIRELESS_DESC_RE = re.compile(
    r"(wi-?fi|wireless|802\.11|wlan|ax2\d\d|ac\s*\d{4}|centrino)",
    re.IGNORECASE,
)

# Core Microsoft Store / Xbox / Auth UWP package family prefixes relevant to Xbox streaming
CORE_UWP_FAMILIES = [
    "microsoft.windowsstore_8wekyb3d8bbwe",
    "microsoft.gamingapp_8wekyb3d8bbwe",
    "microsoft.gamingservices_8wekyb3d8bbwe",
    "microsoft.xboxidentityprovider_8wekyb3d8bbwe",
    "microsoft.xboxgamingoverlay_8wekyb3d8bbwe",
    "microsoft.xboxspeechtotextoverlay_8wekyb3d8bbwe",
    "microsoft.xbox.tcui_8wekyb3d8bbwe",
    "microsoft.aad.brokerplugin_cw5n1h2txyewy",
    "microsoft.accountscontrol_cw5n1h2txyewy",
]

FULL_UWP_EXEMPT_FAMILIES = CORE_UWP_FAMILIES + [
    "microsoft.storepurchaseapp_8wekyb3d8bbwe",
    "microsoft.xboxapp_8wekyb3d8bbwe",
    "microsoft.xboxgameoverlay_8wekyb3d8bbwe",
    "microsoft.desktopappinstaller_8wekyb3d8bbwe",
    "microsoft.ui.xaml.cbs_8wekyb3d8bbwe",
    "microsoft.windows.cloudexperiencehost_cw5n1h2txyewy",
    "microsoft.windows.contentdeliverymanager_cw5n1h2txyewy",
    "microsoft.microsoftedge_8wekyb3d8bbwe",
]

POWERSAVE_KEYWORDS = (
    "*EEE",
    "PowerSavingMode",
    "EnableGreenEthernet",
    "GigaLite",
    "EEELinkAdvertisement",
    "AutoDisableGigabit",
)

_BATCH_PROBE_PS = r"""
$result = [ordered]@{
  os_version = ""
  net_adapters = @()
  pnp_ethernet_devices = @()
  ipv4_addresses = @()
  default_routes = @()
  ip_forwarding = @{}
  ics_sharing_interfaces = @()
  shared_access_service_status = "Unknown"
  udp_listeners = @()
  net_nats = @()
  firewall_rules = @{}
  uwp_installed_core = @()
  uwp_exempt_raw = ""
  xbox_app_installed = $false
}

try {
  $cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
  if ($cv) {
    $build = 0
    try { $build = [int]$cv.CurrentBuild } catch {}
    $osPrefix = if ($build -ge 22000) { "Windows 11" } else { [string]$cv.ProductName }
    $result.os_version = "$osPrefix $($cv.DisplayVersion) (Build $build)"
  }
} catch {}

$virtualRe = '(?i)(vmware|hyper-v|vethernet|wsl|wintun|sing-tun|tailscale|wireguard|openvpn|tap-windows|tunnel|teredo|isatap|loopback|virtualbox|zerotier|lightxtreme|clash|mihomo|bluetooth|miniport|wi-fi direct|kernel debug)'
$wirelessRe = '(?i)(wi-?fi|wireless|802\.11|wlan)'
$psKeys = @('*EEE','PowerSavingMode','EnableGreenEthernet','GigaLite','EEELinkAdvertisement','AutoDisableGigabit')

$adapters = @()
try { $adapters = @(Get-NetAdapter -IncludeHidden:$false -ErrorAction SilentlyContinue) } catch {}

$adapterIndexMap = @{}
foreach ($a in $adapters) {
  $desc = [string]$a.InterfaceDescription
  $name = [string]$a.Name
  $isVirt = ($a.Virtual -eq $true) -or ($desc -match $virtualRe) -or ($name -match $virtualRe)
  $isWifi = ($desc -match $wirelessRe) -or ($name -match $wirelessRe) -or ($a.NdisPhysicalMedium -eq 9)
  $adapterIndexMap[[int]$a.ifIndex] = @{ name = $name; desc = $desc; is_virt = $isVirt; is_wifi = $isWifi }

  $rxErr = 0; $txErr = 0; $rxDisc = 0; $txDisc = 0
  $psEnabled = @()
  if (-not $isVirt -and -not $isWifi) {
    try {
      $st = Get-NetAdapterStatistics -Name $name -ErrorAction SilentlyContinue
      if ($st) {
        $rxErr = [int64]$st.ReceivedPacketErrors
        $txErr = [int64]$st.OutboundPacketErrors
        $rxDisc = [int64]$st.ReceivedDiscardedPackets
        $txDisc = [int64]$st.OutboundDiscardedPackets
      }
    } catch {}
    try {
      $adv = @(Get-NetAdapterAdvancedProperty -Name $name -ErrorAction SilentlyContinue)
      foreach ($p in $adv) {
        if ($psKeys -contains $p.RegistryKeyword) {
          $val = ($p.RegistryValue -join ',')
          $disp = [string]$p.DisplayValue
          if ($val -ne '0' -and $disp -notmatch '(?i)^(disabled|off|禁用|关闭)$') {
            $psEnabled += [string]$p.RegistryKeyword
          }
        }
      }
    } catch {}
  }

  $bps = 0
  try { $bps = [int64]$a.Speed } catch {}
  $result.net_adapters += [ordered]@{
    name = $name
    interface_description = $desc
    interface_index = [int]$a.ifIndex
    status = [string]$a.Status
    link_speed_bps = $bps
    link_speed_text = [string]$a.LinkSpeed
    pnp_device_id = [string]$a.PnPDeviceID
    is_virtual = [bool]$isVirt
    is_wireless = [bool]$isWifi
    rx_errors = $rxErr
    tx_errors = $txErr
    rx_discards = $rxDisc
    tx_discards = $txDisc
    powersave_enabled = @($psEnabled)
  }
}

# Fast targeted PnP check ONLY when physical Ethernet is missing or disabled (avoids 10s full PnP scan)
$physActiveCount = @($result.net_adapters | Where-Object { (-not $_.is_virtual) -and (-not $_.is_wireless) -and ($_.status -ne 'Disabled') }).Count
if ($physActiveCount -eq 0) {
  try {
    $pnpLines = & pnputil /enum-devices /class Net /disconnected /problem 2>$null
    $instIds = @()
    foreach ($line in $pnpLines) {
      if ($line -match '((?:PCI|USB)\\[^\r\n]+)') {
        $candId = $Matches[1].Trim()
        if ($instIds -notcontains $candId) { $instIds += $candId }
      }
    }
    foreach ($a in $result.net_adapters) {
      if ((-not $a.is_virtual) -and (-not $a.is_wireless) -and $a.pnp_device_id) {
        if ($instIds -notcontains $a.pnp_device_id) { $instIds += $a.pnp_device_id }
      }
    }
    foreach ($instId in $instIds) {
      $dev = Get-PnpDevice -InstanceId $instId -ErrorAction SilentlyContinue
      if (-not $dev) { continue }
      $fn = [string]$dev.FriendlyName
      if (($fn -match $virtualRe) -or ($fn -match $wirelessRe)) { continue }
      $prob = 0; $parentId = ""; $parentName = ""; $parentProb = 0; $parentStatus = ""
      try {
        $pProp = Get-PnpDeviceProperty -InstanceId $instId -KeyName 'DEVPKEY_Device_ProblemCode' -ErrorAction SilentlyContinue
        if ($pProp -and $null -ne $pProp.Data) { $prob = [int]$pProp.Data }
      } catch {}
      try {
        $parProp = Get-PnpDeviceProperty -InstanceId $instId -KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue
        if ($parProp -and $parProp.Data) {
          $parentId = [string]$parProp.Data
          $pDev = Get-PnpDevice -InstanceId $parentId -ErrorAction SilentlyContinue
          if ($pDev) {
            $parentName = [string]$pDev.FriendlyName
            $parentStatus = [string]$pDev.Status
            $ppProp = Get-PnpDeviceProperty -InstanceId $parentId -KeyName 'DEVPKEY_Device_ProblemCode' -ErrorAction SilentlyContinue
            if ($ppProp -and $null -ne $ppProp.Data) { $parentProb = [int]$ppProp.Data }
          }
        }
      } catch {}
      $result.pnp_ethernet_devices += [ordered]@{
        friendly_name = $fn
        instance_id = $instId
        status = [string]$dev.Status
        problem_code = $prob
        parent_instance_id = $parentId
        parent_friendly_name = $parentName
        parent_problem_code = $parentProb
        parent_status = $parentStatus
      }
    }
  } catch {}
}

try {
  $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' })
  foreach ($ip in $ips) {
    $result.ipv4_addresses += [ordered]@{
      interface_alias = [string]$ip.InterfaceAlias
      interface_index = [int]$ip.InterfaceIndex
      ip = [string]$ip.IPAddress
      prefix_length = [int]$ip.PrefixLength
    }
  }
} catch {}

try {
  $routes = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
  foreach ($r in $routes) {
    $idx = [int]$r.ifIndex
    $meta = $adapterIndexMap[$idx]
    $desc = if ($meta) { [string]$meta.desc } else { "" }
    $alias = [string]$r.InterfaceAlias
    $isVirt = if ($meta) { [bool]$meta.is_virt } else { ($alias -match $virtualRe) }
    $result.default_routes += [ordered]@{
      interface_alias = $alias
      interface_index = $idx
      next_hop = [string]$r.NextHop
      metric = [int]$r.RouteMetric
      is_virtual_or_tun = $isVirt
      interface_description = $desc
    }
  }
} catch {}

try {
  $ifaces = @(Get-NetIPInterface -AddressFamily IPv4 -ErrorAction SilentlyContinue)
  foreach ($i in $ifaces) {
    $result.ip_forwarding[[string]$i.InterfaceAlias] = ($i.Forwarding -eq 'Enabled')
  }
} catch {}

try {
  $svc = Get-Service -Name 'SharedAccess' -ErrorAction SilentlyContinue
  if ($svc) { $result.shared_access_service_status = [string]$svc.Status }
} catch {}

try {
  $mgr = New-Object -ComObject HNetCfg.HNetShare
  foreach ($conn in $mgr.EnumEveryConnection) {
    $cfg = $mgr.INetSharingConfigurationForINetConnection.Invoke($conn)
    if ($cfg.SharingEnabled) {
      $props = $mgr.NetConnectionProps.Invoke($conn)
      $result.ics_sharing_interfaces += [ordered]@{
        name = [string]$props.Name
        sharing_type = [int]$cfg.SharingConnectionType
      }
    }
  }
} catch {}

try {
  $udps = @(Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in @(53, 67) })
  $pidSvcCache = @{}
  foreach ($u in $udps) {
    $procId = [int]$u.OwningProcess
    $procName = ""
    $svcName = ""
    try {
      $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
      if ($p) { $procName = [string]$p.ProcessName }
    } catch {}
    if (-not $pidSvcCache.ContainsKey($procId)) {
      $svcs = @()
      try {
        $svcs = @(Get-CimInstance Win32_Service -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
      } catch {}
      $pidSvcCache[$procId] = ($svcs -join ',')
    }
    $svcName = [string]$pidSvcCache[$procId]
    $result.udp_listeners += [ordered]@{
      port = [int]$u.LocalPort
      pid = $procId
      process_name = $procName
      service_name = $svcName
    }
  }
} catch {}

try {
  $nats = @(Get-NetNat -ErrorAction SilentlyContinue)
  foreach ($n in $nats) {
    $result.net_nats += [ordered]@{
      name = [string]$n.Name
      internal_subnet = [string]$n.InternalIPInterfaceAddressPrefix
    }
  }
} catch {}

try {
  $fwPolicy = New-Object -ComObject HNetCfg.FwPolicy2
  foreach ($rName in @('__FW_NAT__', '__FW_DIRECT__')) {
    $exists = $false
    try {
      $null = $fwPolicy.Rules.Item($rName)
      $exists = $true
    } catch {}
    $result.firewall_rules[$rName] = $exists
  }
} catch {
  foreach ($rName in @('__FW_NAT__', '__FW_DIRECT__')) {
    $result.firewall_rules[$rName] = $false
  }
}

try {
  $corePkgs = @(Get-AppxPackage -ErrorAction SilentlyContinue | Where-Object {
    $_.PackageFamilyName -match '(?i)^(microsoft\.windowsstore_|microsoft\.gamingapp_|microsoft\.gamingservices_|microsoft\.xbox|microsoft\.aad\.brokerplugin_|microsoft\.accountscontrol_)'
  })
  foreach ($pkg in $corePkgs) {
    $pfn = ([string]$pkg.PackageFamilyName).ToLowerInvariant()
    $result.uwp_installed_core += $pfn
    if ($pfn -like 'microsoft.gamingapp_*') { $result.xbox_app_installed = $true }
  }
} catch {}

try {
  $exemptLines = & CheckNetIsolation.exe LoopbackExempt -s 2>$null
  $result.uwp_exempt_raw = ($exemptLines -join "`n")
} catch {}

$result | ConvertTo-Json -Depth 6 -Compress
"""


def parse_uwp_exemptions(raw_output: str) -> list[str]:
    """Extract lowercase PackageFamilyNames from CheckNetIsolation output (locale-independent)."""
    found: set[str] = set()
    for line in raw_output.splitlines():
        for m in re.finditer(r"\b([A-Za-z0-9][A-Za-z0-9.-]*_[A-Za-z0-9]{13})\b", line):
            found.add(m.group(1).strip().lower())
    return sorted(found)


def detect_local_xstreaming_path() -> str | None:
    """Scan standard Windows user and machine locations for XStreamingDesktop."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    prog_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    candidates = [
        Path(local_app) / "Programs" / "XStreaming" / "XStreaming.exe",
        Path(local_app) / "Programs" / "XStreamingDesktop" / "XStreamingDesktop.exe",
        Path(local_app) / "Programs" / "xstreaming" / "XStreaming.exe",
        Path(local_app) / "XStreaming" / "XStreaming.exe",
        Path(prog_files) / "XStreaming" / "XStreaming.exe",
        Path(prog_files) / "XStreamingDesktop" / "XStreamingDesktop.exe",
        Path(prog_files_x86) / "XStreaming" / "XStreaming.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)

    # Also check wildcard under %LOCALAPPDATA%\Programs for XStreaming*
    if local_app:
        programs_dir = Path(local_app) / "Programs"
        if programs_dir.is_dir():
            for child in programs_dir.iterdir():
                if child.is_dir() and "xstreaming" in child.name.lower():
                    for exe in child.glob("*.exe"):
                        if "unins" not in exe.name.lower():
                            return str(exe)
    return None


def parse_ping_output(out: str) -> tuple[bool, float | None]:
    """Parse Windows ping output across English/Chinese locales and reject unreachable rc=0."""
    m = re.search(r"(?:time|时间)\s*([<=])\s*(\d+(?:\.\d+)?)\s*ms", out, re.IGNORECASE)
    if m:
        val = float(m.group(2))
        if m.group(1) == "<" and val <= 1.0:
            return True, 0.5
        return True, val
    if re.search(r"\bTTL\s*=\s*\d+", out, re.IGNORECASE):
        return True, None
    return False, None


def _windows_oem_encoding() -> str:
    if os.name == "nt":
        try:
            import ctypes

            cp = int(ctypes.windll.kernel32.GetOEMCP())
            if cp > 0:
                return f"cp{cp}"
        except Exception:
            pass
    import locale

    return locale.getpreferredencoding(False) or "utf-8"


def _ping_peer(peer_ip: str, timeout_ms: int = 600) -> tuple[bool, float | None]:
    creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), peer_ip],
            capture_output=True,
            timeout=2.5,
            creationflags=creationflags,
        )
        if proc.returncode != 0:
            return False, None
        raw = proc.stdout or b""
        enc = _windows_oem_encoding()
        out = raw.decode(enc, errors="replace")
        ok, rtt = parse_ping_output(out)
        if not ok and enc.lower() != "utf-8":
            ok, rtt = parse_ping_output(raw.decode("utf-8", errors="replace"))
        return ok, rtt
    except Exception:
        return False, None


def _http_probe(url: str, timeout: float = 2.0) -> tuple[bool, int]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"WireDeck/{APP_VERSION}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, int(resp.status)
    except urllib.error.HTTPError as exc:
        # 405 Method Not Allowed on Xbox POST-only auth endpoint confirms TLS reachability
        return (200 <= exc.code < 500), int(exc.code)
    except Exception:
        return False, 0


def _find_first_file(candidates: list[Path]) -> str | None:
    for p in candidates:
        try:
            if p.is_file():
                return str(p)
        except OSError:
            continue
    return None


def detect_local_ps_clients() -> dict[str, str | None]:
    """Detect Chiaki-ng / Chiaki and Sony PS Remote Play installations."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    chiaki = _find_first_file([
        Path(pf) / "chiaki-ng" / "chiaki.exe",
        Path(pf) / "Chiaki-ng" / "chiaki.exe",
        Path(pf) / "chiaki" / "chiaki.exe",
        Path(local_app) / "Programs" / "chiaki-ng" / "chiaki.exe",
        Path(local_app) / "chiaki-ng" / "chiaki.exe",
    ])
    ps_remote = _find_first_file([
        Path(pf86) / "Sony" / "PS Remote Play" / "RemotePlay.exe",
        Path(pf) / "Sony" / "PS Remote Play" / "RemotePlay.exe",
    ])
    return {"chiaki_path": chiaki, "ps_remote_path": ps_remote}


def detect_local_handheld_clients() -> dict[str, str | None]:
    """Detect Moonlight, Sunshine, and Steam installations for handheld streaming."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    moonlight = _find_first_file([
        Path(pf) / "Moonlight Game Streaming" / "Moonlight.exe",
        Path(pf86) / "Moonlight Game Streaming" / "Moonlight.exe",
        Path(local_app) / "Programs" / "Moonlight" / "Moonlight.exe",
    ])
    sunshine = _find_first_file([
        Path(pf) / "Sunshine" / "sunshine.exe",
        Path(pf86) / "Sunshine" / "sunshine.exe",
    ])
    steam = _find_first_file([
        Path(pf86) / "Steam" / "steam.exe",
        Path(pf) / "Steam" / "steam.exe",
    ])
    return {"moonlight_path": moonlight, "sunshine_path": sunshine, "steam_path": steam}


def probe_connectivity(cfg: AppConfig) -> dict[str, Any]:
    """Run bounded concurrent peer ping, host internet, and preset service endpoint probes."""
    preset = get_preset(cfg.active_preset)
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_peer = pool.submit(_ping_peer, cfg.peer_ip, 600)
        f_inet = pool.submit(_http_probe, "http://www.msftconnecttest.com/connecttest.txt", 2.0)
        f_svc = (
            pool.submit(_http_probe, preset.service_endpoint_url, 2.5)
            if preset.service_endpoint_url
            else None
        )
        peer_ok, peer_rtt = f_peer.result()
        inet_ok, _ = f_inet.result()
        if not inet_ok:
            inet_ok, _ = _http_probe("https://login.microsoftonline.com/", 2.0)
        if f_svc is not None:
            svc_ok, svc_status = f_svc.result()
        else:
            svc_ok, svc_status = None, None

    return {
        "peer_reachable": peer_ok,
        "peer_rtt_ms": peer_rtt,
        "host_internet_ok": inet_ok,
        "xbox_live_ok": svc_ok if preset.id == "xbox" else None,
        "xbox_live_status": svc_status if preset.id == "xbox" else None,
        "service_endpoint_ok": svc_ok,
        "service_endpoint_status": svc_status,
    }


def collect_system_snapshot(
    cfg: AppConfig,
    runner: Callable[[str, int], PsResult] = run_powershell,
    *,
    probe_network: bool = True,
    network_probe_fn: Callable[[AppConfig], dict[str, Any]] = probe_connectivity,
) -> dict[str, Any]:
    """Collect a complete read-only snapshot of local Windows networking and streaming state."""
    ps_script = _BATCH_PROBE_PS.replace("__FW_NAT__", FW_RULE_NAT).replace(
        "__FW_DIRECT__", FW_RULE_DIRECT
    )

    # Run PowerShell probe and network connectivity probes concurrently to cut cold-start latency
    if probe_network:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_ps = pool.submit(runner, ps_script, 15)
            f_net = pool.submit(network_probe_fn, cfg)
            ps_res = f_ps.result()
            connectivity = f_net.result()
    else:
        ps_res = runner(ps_script, 15)
        connectivity = {
            "peer_reachable": None,
            "peer_rtt_ms": None,
            "host_internet_ok": None,
            "xbox_live_ok": None,
            "xbox_live_status": None,
        }

    parsed: dict[str, Any] = {}
    if ps_res.stdout.strip():
        # Strip any accidental leading non-JSON lines from PowerShell profiles/warnings
        text = ps_res.stdout.strip()
        first_brace = text.find("{")
        if first_brace >= 0:
            try:
                parsed = json.loads(text[first_brace:])
            except json.JSONDecodeError:
                parsed = {"probe_parse_error": text[:300]}

    if not isinstance(parsed, dict):
        parsed = {}

    exempt_packages = parse_uwp_exemptions(str(parsed.get("uwp_exempt_raw", "")))
    installed_core = [str(p).lower() for p in (parsed.get("uwp_installed_core") or [])]
    target_core = [p for p in CORE_UWP_FAMILIES if p in installed_core] if installed_core else []
    missing_uwp = [p for p in target_core if p not in exempt_packages]

    return {
        "os_version": parsed.get("os_version", ""),
        "is_admin": is_admin(),
        "probe_error": ps_res.stderr.strip() if ps_res.returncode != 0 else "",
        "net_adapters": parsed.get("net_adapters") or [],
        "pnp_ethernet_devices": parsed.get("pnp_ethernet_devices") or [],
        "ipv4_addresses": parsed.get("ipv4_addresses") or [],
        "default_routes": parsed.get("default_routes") or [],
        "ip_forwarding": parsed.get("ip_forwarding") or {},
        "ics_sharing_interfaces": parsed.get("ics_sharing_interfaces") or [],
        "shared_access_service_status": parsed.get("shared_access_service_status", "Unknown"),
        "udp_listeners": parsed.get("udp_listeners") or [],
        "net_nats": parsed.get("net_nats") or [],
        "firewall_rules": parsed.get("firewall_rules") or {},
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
