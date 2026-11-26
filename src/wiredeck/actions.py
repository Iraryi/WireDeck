"""Privileged system actions with strict L3 gates, NIC snapshots, and UTF-8 BOM logging."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from wiredeck.config import AppConfig
from wiredeck.diagnostics import classify_snapshot, select_ethernet_adapter, select_uplink_adapter
from wiredeck.models import ActionResult, Severity
from wiredeck.paths import AppPaths
from wiredeck.probes import (
    FULL_UWP_EXEMPT_FAMILIES,
    POWERSAVE_KEYWORDS,
    collect_system_snapshot,
)
from wiredeck.ps_runner import PsResult, run_powershell
from wiredeck.version import FW_RULE_DIRECT, FW_RULE_NAT, NAT_NAME

SUPPORTED_ACTIONS = (
    "enable-direct",
    "disable-direct",
    "reset-nic",
    "fix-nic",
    "fix-uwp",
    "clear-ics",
    "clear-netnat",
    "run-ext",
)

PRIVILEGED_ACTIONS = frozenset(SUPPORTED_ACTIONS)
MAX_LOG_FILES = 14


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _append_bom_log(paths: AppPaths, action: str, lines: list[str]) -> str:
    """Append timestamped action trace to daily UTF-8 BOM + CRLF log file."""
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    log_path = paths.logs_dir / f"wiredeck_{day}.log"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block = [f"[{stamp}] ACTION={action}"] + [f"  {line}" for line in lines] + [""]
    text = "\r\n".join(block)

    if not log_path.exists():
        log_path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    else:
        with log_path.open("ab") as fh:
            fh.write(text.encode("utf-8"))

    # Keep only latest MAX_LOG_FILES daily logs
    logs = sorted(paths.logs_dir.glob("wiredeck_*.log"))
    for stale in logs[:-MAX_LOG_FILES]:
        try:
            stale.unlink()
        except OSError:
            pass
    return str(log_path)


def _save_nic_backup_once(
    paths: AppPaths,
    eth_name: str,
    snapshot: dict[str, Any],
    runner: Callable[[str, int], PsResult],
) -> None:
    """Snapshot original Ethernet IPv4/DHCP settings once before WireDeck modifies it."""
    backup_path = paths.runtime_dir / "nic_backup.json"
    if backup_path.is_file():
        return

    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    ps_query = f"""
$alias = {_ps_quote(eth_name)}
$iface = Get-NetIPInterface -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue
$ips = @(Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object {{ "$($_.IPAddress)/$($_.PrefixLength)" }})
$dns = @((Get-DnsClientServerAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue).ServerAddresses)
[ordered]@{{
  interface_alias = $alias
  dhcp = if ($iface) {{ $iface.Dhcp -eq 'Enabled' }} else {{ $true }}
  ips = $ips
  dns = $dns
}} | ConvertTo-Json -Depth 4 -Compress
"""
    res = runner(ps_query, 15)
    backup_data: dict[str, Any] = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "interface_alias": eth_name,
        "dhcp": True,
        "ips": [
            f"{ip.get('ip')}/{ip.get('prefix_length')}"
            for ip in (snapshot.get("ipv4_addresses") or [])
            if ip.get("interface_alias") == eth_name
        ],
        "dns": [],
    }
    if res.returncode == 0 and res.stdout.strip():
        try:
            parsed = json.loads(res.stdout.strip()[res.stdout.strip().find("{") :])
            if isinstance(parsed, dict):
                backup_data.update(parsed)
        except Exception:
            pass

    fd, tmp_name = tempfile.mkstemp(prefix=".nic_bk_", suffix=".tmp", dir=str(paths.runtime_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(backup_data, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp_name, backup_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _save_direct_state(paths: AppPaths, eth_name: str, uplink_name: str) -> None:
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    state_path = paths.runtime_dir / "direct_state.json"
    managed = [n for n in (eth_name, uplink_name) if n]
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "eth_interface": eth_name,
        "uplink_interface": uplink_name,
        "forwarding_interfaces": managed,
    }
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_direct_state_interfaces(paths: AppPaths) -> list[str]:
    state_path = paths.runtime_dir / "direct_state.json"
    if not state_path.is_file():
        return []
    try:
        parsed = json.loads(state_path.read_text(encoding="utf-8-sig"))
        if isinstance(parsed, dict):
            raw = parsed.get("forwarding_interfaces") or []
            if isinstance(raw, list):
                return [str(x) for x in raw if isinstance(x, str) and x.strip()]
    except Exception:
        pass
    return []


def _enable_direct(
    paths: AppPaths,
    cfg: AppConfig,
    runner: Callable[[str, int], PsResult],
    snapshot_fn: Callable[..., dict[str, Any]],
) -> ActionResult:
    snapshot = snapshot_fn(cfg, probe_network=False)
    ext_results = []
    try:
        from wiredeck.extensions import run_auto_check_extensions

        ext_results = [
            r
            for r in run_auto_check_extensions(paths)
            if r.layer in ("L1", "L3") and r.code != "EXT_ERROR"
        ]
    except Exception:
        ext_results = []

    report = classify_snapshot(snapshot, cfg, ext_results=ext_results)

    l3 = next(l for l in report.layers if l.layer == "L3")
    l3_block = next((f for f in l3.faults if f.severity == Severity.FAIL), None)
    if l3_block is not None:
        log_file = _append_bom_log(
            paths, "enable-direct", [f"BLOCKED by L3: {l3_block.code} - {l3_block.message}"]
        )
        return ActionResult(
            ok=False,
            code=l3_block.code,
            message=l3_block.message,
            exit_code=2,
            details=l3_block.details,
            log_file=log_file,
        )

    l1 = next(l for l in report.layers if l.layer == "L1")
    if l1.status == Severity.FAIL:
        l1_fail = next((f for f in l1.faults if f.severity == Severity.FAIL), None)
        code = l1_fail.code if l1_fail else "NIC_NOT_READY"
        msg = l1_fail.message if l1_fail else "有线网卡未就绪，请先修复网卡"
        log_file = _append_bom_log(paths, "enable-direct", [f"BLOCKED by L1: {code} - {msg}"])
        return ActionResult(ok=False, code=code, message=msg, exit_code=2, log_file=log_file)

    eth = select_ethernet_adapter(
        snapshot.get("net_adapters") or [],
        cfg.ethernet_interface,
        default_routes=snapshot.get("default_routes") or [],
    )
    if eth is None:
        log_file = _append_bom_log(paths, "enable-direct", ["ERROR: no Ethernet adapter found"])
        return ActionResult(
            ok=False, code="NIC_NOT_FOUND", message="未检测到可用有线网卡", exit_code=2, log_file=log_file
        )

    eth_name = str(eth["name"])
    uplink = select_uplink_adapter(
        snapshot.get("net_adapters") or [],
        snapshot.get("default_routes") or [],
        eth_name,
        cfg.uplink_interface,
    )
    uplink_name = str(uplink["name"]) if uplink else ""

    _save_nic_backup_once(paths, eth_name, snapshot, runner)

    uplink_fwd_on = (
        f"  Set-NetIPInterface -InterfaceAlias {_ps_quote(uplink_name)} -AddressFamily IPv4 -Forwarding Enabled"
        if uplink_name
        else "  # No distinct uplink interface detected"
    )
    uplink_fwd_rollback = (
        f"  Set-NetIPInterface -InterfaceAlias {_ps_quote(uplink_name)} -AddressFamily IPv4 -Forwarding Disabled -ErrorAction SilentlyContinue"
        if uplink_name
        else ""
    )

    script = f"""
$eth = {_ps_quote(eth_name)}
$natName = {_ps_quote(NAT_NAME)}
$subnet = {_ps_quote(cfg.subnet_cidr)}
$hostIp = {_ps_quote(cfg.host_ip)}
$prefix = {int(cfg.prefix_length)}
$fwNat = {_ps_quote(FW_RULE_NAT)}
$fwDirect = {_ps_quote(FW_RULE_DIRECT)}

# Remove previous WireDeckNat only (never touch foreign NetNats)
Get-NetNat -Name $natName -ErrorAction SilentlyContinue | Remove-NetNat -Confirm:$false -ErrorAction SilentlyContinue

# Configure static IPv4 on wired NIC with NO default gateway
Set-NetIPInterface -InterfaceAlias $eth -AddressFamily IPv4 -Dhcp Disabled -ErrorAction SilentlyContinue
Get-NetRoute -InterfaceAlias $eth -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
$existing = @(Get-NetIPAddress -InterfaceAlias $eth -AddressFamily IPv4 -ErrorAction SilentlyContinue)
$hasTarget = $false
foreach ($ip in $existing) {{
  if ($ip.IPAddress -eq $hostIp -and [int]$ip.PrefixLength -eq $prefix) {{
    $hasTarget = $true
  }} else {{
    Remove-NetIPAddress -InterfaceAlias $eth -IPAddress $ip.IPAddress -Confirm:$false -ErrorAction SilentlyContinue
  }}
}}
if (-not $hasTarget) {{
  New-NetIPAddress -InterfaceAlias $eth -IPAddress $hostIp -PrefixLength $prefix -ErrorAction Stop | Out-Null
}}
Set-DnsClientServerAddress -InterfaceAlias $eth -ResetServerAddresses -ErrorAction SilentlyContinue

try {{
  # Enable IPv4 forwarding on wired + uplink interfaces
  Set-NetIPInterface -InterfaceAlias $eth -AddressFamily IPv4 -Forwarding Enabled
{uplink_fwd_on}

  # Create clean WinNAT instance
  New-NetNat -Name $natName -InternalIPInterfaceAddressPrefix $subnet | Out-Null

  # Ensure scoped firewall rules for the direct subnet only
  Remove-NetFirewallRule -DisplayName $fwNat -ErrorAction SilentlyContinue
  New-NetFirewallRule -DisplayName $fwNat -Direction Inbound -Action Allow -RemoteAddress $subnet -Profile Any -Enabled True | Out-Null
  Remove-NetFirewallRule -DisplayName $fwDirect -ErrorAction SilentlyContinue
  New-NetFirewallRule -DisplayName $fwDirect -Direction Outbound -Action Allow -RemoteAddress $subnet -Profile Any -Enabled True | Out-Null
}} catch {{
  # Roll back partial Forwarding/NAT state so a mid-flight failure never leaves dirty routing
  Get-NetNat -Name $natName -ErrorAction SilentlyContinue | Remove-NetNat -Confirm:$false -ErrorAction SilentlyContinue
  Remove-NetFirewallRule -DisplayName $fwNat -ErrorAction SilentlyContinue
  Remove-NetFirewallRule -DisplayName $fwDirect -ErrorAction SilentlyContinue
  Set-NetIPInterface -InterfaceAlias $eth -AddressFamily IPv4 -Forwarding Disabled -ErrorAction SilentlyContinue
{uplink_fwd_rollback}
  throw
}}

Write-Output "ENABLE_DIRECT_OK"
"""
    res = runner(script, 30)
    log_file = _append_bom_log(
        paths,
        "enable-direct",
        [
            f"eth={eth_name}, uplink={uplink_name}, host_ip={cfg.host_ip}/{cfg.prefix_length}, subnet={cfg.subnet_cidr}",
            f"returncode={res.returncode}",
            f"stdout={res.stdout.strip()}",
            f"stderr={res.stderr.strip()}",
        ],
    )
    if res.returncode != 0:
        return ActionResult(
            ok=False,
            code="ENABLE_DIRECT_FAILED",
            message="开启游戏直连失败，已回滚转发设置，请查看日志",
            exit_code=1,
            details={"stderr": res.stderr.strip()},
            log_file=log_file,
        )
    _save_direct_state(paths, eth_name, uplink_name)
    return ActionResult(
        ok=True,
        code="DIRECT_ENABLED",
        message="千兆游戏直连已开启",
        exit_code=0,
        details={"eth": eth_name, "uplink": uplink_name, "subnet": cfg.subnet_cidr},
        log_file=log_file,
    )


def _disable_direct(
    paths: AppPaths,
    cfg: AppConfig,
    runner: Callable[[str, int], PsResult],
    snapshot_fn: Callable[..., dict[str, Any]],
) -> ActionResult:
    snapshot = snapshot_fn(cfg, probe_network=False)
    eth = select_ethernet_adapter(
        snapshot.get("net_adapters") or [],
        cfg.ethernet_interface,
        default_routes=snapshot.get("default_routes") or [],
    )
    eth_name = str(eth["name"]) if eth else cfg.ethernet_interface
    uplink = select_uplink_adapter(
        snapshot.get("net_adapters") or [],
        snapshot.get("default_routes") or [],
        eth_name,
        cfg.uplink_interface,
    )
    uplink_name = str(uplink["name"]) if uplink else cfg.uplink_interface

    # Strictly scope Forwarding Disabled to WireDeck-managed physical interfaces ONLY
    # (never disable Forwarding on third-party VPN TUN, WSL, Hyper-V, or VMware adapters!)
    fwd_targets: list[str] = []
    for name in (*_load_direct_state_interfaces(paths), eth_name, uplink_name):
        if name and name not in fwd_targets:
            fwd_targets.append(name)

    disable_fwd_lines = "\n".join(
        f"Set-NetIPInterface -InterfaceAlias {_ps_quote(name)} -AddressFamily IPv4 -Forwarding Disabled -ErrorAction SilentlyContinue"
        for name in fwd_targets
    ) or "# No WireDeck-managed interfaces detected"

    # Intentionally preserve static 192.168.50.2/24 (no gateway) for instant re-toggle
    script = f"""
$natName = {_ps_quote(NAT_NAME)}
$fwNat = {_ps_quote(FW_RULE_NAT)}
$fwDirect = {_ps_quote(FW_RULE_DIRECT)}

Get-NetNat -Name $natName -ErrorAction SilentlyContinue | Remove-NetNat -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName $fwNat -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName $fwDirect -ErrorAction SilentlyContinue
{disable_fwd_lines}

Write-Output "DISABLE_DIRECT_OK"
"""
    res = runner(script, 30)
    if res.returncode == 0:
        try:
            (paths.runtime_dir / "direct_state.json").unlink(missing_ok=True)
        except OSError:
            pass
    log_file = _append_bom_log(
        paths,
        "disable-direct",
        [
            f"fwd_disabled={fwd_targets}",
            f"returncode={res.returncode}",
            f"stdout={res.stdout.strip()}",
            f"stderr={res.stderr.strip()}",
        ],
    )
    if res.returncode != 0:
        return ActionResult(
            ok=False,
            code="DISABLE_DIRECT_FAILED",
            message="恢复日常上网失败，请查看日志",
            exit_code=1,
            details={"stderr": res.stderr.strip()},
            log_file=log_file,
        )
    return ActionResult(
        ok=True,
        code="DIRECT_DISABLED",
        message="已恢复日常上网模式（保留本地静态直连 IP）",
        exit_code=0,
        details={"forwarding_disabled": fwd_targets},
        log_file=log_file,
    )


def _reset_nic(
    paths: AppPaths,
    cfg: AppConfig,
    runner: Callable[[str, int], PsResult],
    snapshot_fn: Callable[..., dict[str, Any]],
) -> ActionResult:
    snapshot = snapshot_fn(cfg, probe_network=False)
    eth = select_ethernet_adapter(
        snapshot.get("net_adapters") or [],
        cfg.ethernet_interface,
        default_routes=snapshot.get("default_routes") or [],
    )
    eth_name = str(eth["name"]) if eth else cfg.ethernet_interface
    uplink = select_uplink_adapter(
        snapshot.get("net_adapters") or [],
        snapshot.get("default_routes") or [],
        eth_name,
        cfg.uplink_interface,
    )
    uplink_name = str(uplink["name"]) if uplink else cfg.uplink_interface

    backup_path = paths.runtime_dir / "nic_backup.json"
    backup_data: dict[str, Any] | None = None
    if backup_path.is_file():
        try:
            parsed = json.loads(backup_path.read_text(encoding="utf-8-sig"))
            if isinstance(parsed, dict):
                backup_data = parsed
        except Exception:
            backup_data = None

    if not eth_name and backup_data and backup_data.get("interface_alias"):
        eth_name = str(backup_data["interface_alias"])
    if not eth_name:
        log_file = _append_bom_log(paths, "reset-nic", ["ERROR: no Ethernet interface to reset"])
        return ActionResult(
            ok=False, code="NIC_NOT_FOUND", message="未找到可重置的有线网卡", exit_code=1, log_file=log_file
        )

    fwd_targets: list[str] = []
    for name in (*_load_direct_state_interfaces(paths), eth_name, uplink_name):
        if name and name not in fwd_targets:
            fwd_targets.append(name)
    disable_fwd_lines = "\n".join(
        f"Set-NetIPInterface -InterfaceAlias {_ps_quote(name)} -AddressFamily IPv4 -Forwarding Disabled -ErrorAction SilentlyContinue"
        for name in fwd_targets
    )

    restore_static = (
        backup_data is not None
        and backup_data.get("dhcp") is False
        and isinstance(backup_data.get("ips"), list)
        and len(backup_data["ips"]) > 0
    )

    if restore_static and backup_data is not None:
        static_lines: list[str] = [
            "Get-NetIPAddress -InterfaceAlias $eth -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue",
            "Set-NetIPInterface -InterfaceAlias $eth -AddressFamily IPv4 -Dhcp Disabled -ErrorAction SilentlyContinue",
        ]
        for cidr in backup_data["ips"]:
            if isinstance(cidr, str) and "/" in cidr:
                ip_part, pfx_part = cidr.split("/", 1)
                if ip_part.strip() and pfx_part.strip().isdigit():
                    static_lines.append(
                        f"New-NetIPAddress -InterfaceAlias $eth -IPAddress {_ps_quote(ip_part.strip())} -PrefixLength {int(pfx_part.strip())} -ErrorAction SilentlyContinue | Out-Null"
                    )
        saved_dns = [str(d).strip() for d in (backup_data.get("dns") or []) if isinstance(d, str) and d.strip()]
        if saved_dns:
            dns_lit = ",".join(_ps_quote(d) for d in saved_dns)
            static_lines.append(
                f"Set-DnsClientServerAddress -InterfaceAlias $eth -ServerAddresses @({dns_lit}) -ErrorAction SilentlyContinue"
            )
        else:
            static_lines.append(
                "Set-DnsClientServerAddress -InterfaceAlias $eth -ResetServerAddresses -ErrorAction SilentlyContinue"
            )
        ip_restore_block = "\n".join(static_lines)
    else:
        ip_restore_block = (
            "Get-NetIPAddress -InterfaceAlias $eth -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.PrefixOrigin -eq 'Manual' } | Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue\n"
            "Set-NetIPInterface -InterfaceAlias $eth -AddressFamily IPv4 -Dhcp Enabled -ErrorAction SilentlyContinue\n"
            "Set-DnsClientServerAddress -InterfaceAlias $eth -ResetServerAddresses -ErrorAction SilentlyContinue"
        )

    script = f"""
$eth = {_ps_quote(eth_name)}
Get-NetNat -Name {_ps_quote(NAT_NAME)} -ErrorAction SilentlyContinue | Remove-NetNat -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName {_ps_quote(FW_RULE_NAT)} -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName {_ps_quote(FW_RULE_DIRECT)} -ErrorAction SilentlyContinue
{disable_fwd_lines}
{ip_restore_block}
Write-Output "RESET_NIC_OK"
"""
    res = runner(script, 30)
    if res.returncode == 0:
        for stale in (backup_path, paths.runtime_dir / "direct_state.json"):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
    log_file = _append_bom_log(
        paths,
        "reset-nic",
        [
            f"eth={eth_name}, restore_static={restore_static}, fwd_disabled={fwd_targets}",
            f"returncode={res.returncode}",
            f"stderr={res.stderr.strip()}",
        ],
    )
    if res.returncode != 0:
        return ActionResult(
            ok=False,
            code="RESET_NIC_FAILED",
            message="恢复网卡配置失败",
            exit_code=1,
            details={"stderr": res.stderr.strip()},
            log_file=log_file,
        )
    msg = (
        f"已按备份快照恢复 {eth_name} 原有静态配置"
        if restore_static
        else f"已将 {eth_name} 恢复为出厂 DHCP 自动获取"
    )
    return ActionResult(
        ok=True,
        code="NIC_RESET",
        message=msg,
        exit_code=0,
        details={"eth": eth_name, "restored_static": restore_static},
        log_file=log_file,
    )


def _fix_nic(
    paths: AppPaths,
    cfg: AppConfig,
    runner: Callable[[str, int], PsResult],
    snapshot_fn: Callable[..., dict[str, Any]],
) -> ActionResult:
    snapshot = snapshot_fn(cfg, probe_network=False)
    pnp_devices = list(snapshot.get("pnp_ethernet_devices") or [])
    eth = select_ethernet_adapter(snapshot.get("net_adapters") or [], cfg.ethernet_interface)
    eth_name = str(eth["name"]) if eth else ""

    lines: list[str] = []
    # 1. Re-enable disabled parent PCIe Root Ports first
    for dev in pnp_devices:
        if int(dev.get("parent_problem_code") or 0) == 22 and dev.get("parent_instance_id"):
            pid = _ps_quote(str(dev["parent_instance_id"]))
            lines.append(f"Enable-PnpDevice -InstanceId {pid} -Confirm:$false -ErrorAction SilentlyContinue")
            lines.append("Start-Sleep -Seconds 2")
        if int(dev.get("problem_code") or 0) == 22 and dev.get("instance_id"):
            did = _ps_quote(str(dev["instance_id"]))
            lines.append(f"Enable-PnpDevice -InstanceId {did} -Confirm:$false -ErrorAction SilentlyContinue")

    # 2. Enable disabled Ethernet adapter(s) and turn off power-saving/EEE flags
    ps_keys_literal = ",".join(_ps_quote(k) for k in POWERSAVE_KEYWORDS)
    if eth_name:
        lines.append(f"$targetName = {_ps_quote(eth_name)}")
        lines.append("Enable-NetAdapter -Name $targetName -Confirm:$false -ErrorAction SilentlyContinue")
        lines.append(
            f"$targets = @(Get-NetAdapter -Name $targetName -ErrorAction SilentlyContinue)"
        )
    else:
        lines.append(
            "$targets = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { (-not $_.Virtual) -and ($_.InterfaceDescription -notmatch '(?i)(wi-?fi|wireless|bluetooth|vmware|hyper-v|wintun|sing-tun|tailscale)') })"
        )
        lines.append("foreach ($t in $targets) { Enable-NetAdapter -Name $t.Name -Confirm:$false -ErrorAction SilentlyContinue }")

    lines.append(f"$psKeys = @({ps_keys_literal})")
    lines.append(
        """foreach ($t in $targets) {
  foreach ($k in $psKeys) {
    $prop = Get-NetAdapterAdvancedProperty -Name $t.Name -RegistryKeyword $k -ErrorAction SilentlyContinue
    if ($prop) {
      Set-NetAdapterAdvancedProperty -Name $t.Name -RegistryKeyword $k -RegistryValue 0 -NoRestart -ErrorAction SilentlyContinue
    }
  }
}
Write-Output "FIX_NIC_OK"
"""
    )

    script = "\n".join(lines)
    res = runner(script, 30)
    log_file = _append_bom_log(
        paths,
        "fix-nic",
        [f"eth={eth_name}", f"returncode={res.returncode}", f"stderr={res.stderr.strip()}"],
    )
    if res.returncode != 0:
        return ActionResult(
            ok=False,
            code="FIX_NIC_FAILED",
            message="网卡唤醒或防卡顿优化失败",
            exit_code=1,
            details={"stderr": res.stderr.strip()},
            log_file=log_file,
        )
    return ActionResult(
        ok=True,
        code="NIC_FIXED",
        message="有线网卡已启用并关闭 EEE 节能防卡顿项",
        exit_code=0,
        log_file=log_file,
    )


def _fix_uwp(
    paths: AppPaths,
    cfg: AppConfig,
    runner: Callable[[str, int], PsResult],
) -> ActionResult:
    pkgs_literal = ",".join(_ps_quote(p) for p in FULL_UWP_EXEMPT_FAMILIES)
    script = f"""
$pkgs = @({pkgs_literal})
$okCount = 0
foreach ($p in $pkgs) {{
  & CheckNetIsolation.exe LoopbackExempt -a "-n=$p" 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {{ $okCount++ }}
}}
Write-Output "FIX_UWP_OK:$okCount"
"""
    res = runner(script, 30)
    log_file = _append_bom_log(
        paths,
        "fix-uwp",
        [f"packages={len(FULL_UWP_EXEMPT_FAMILIES)}", f"returncode={res.returncode}", f"stdout={res.stdout.strip()}"],
    )
    if res.returncode != 0:
        return ActionResult(
            ok=False,
            code="FIX_UWP_FAILED",
            message="UWP 回环豁免写入失败",
            exit_code=1,
            details={"stderr": res.stderr.strip()},
            log_file=log_file,
        )
    return ActionResult(
        ok=True,
        code="UWP_FIXED",
        message="已完成微软商店与 Xbox 应用回环豁免",
        exit_code=0,
        log_file=log_file,
    )


def _clear_ics(
    paths: AppPaths,
    cfg: AppConfig,
    runner: Callable[[str, int], PsResult],
    snapshot_fn: Callable[..., dict[str, Any]],
) -> ActionResult:
    snapshot = snapshot_fn(cfg, probe_network=False)
    ics_active = bool(snapshot.get("ics_sharing_interfaces"))
    ics_on_53_67 = any(
        "sharedaccess" in str(u.get("service_name", "")).lower()
        for u in (snapshot.get("udp_listeners") or [])
    )

    # Strictly disable ICS COM flags and stop SharedAccess ONLY if ICS was active or owned 53/67.
    # NEVER terminate third-party non-ICS DNS processes (SmartDNS/AdGuard)!
    stop_svc = (
        "Stop-Service -Name 'SharedAccess' -Force -ErrorAction SilentlyContinue"
        if (ics_active or ics_on_53_67)
        else "# SharedAccess not actively holding ICS/53/67; leaving idle service untouched"
    )
    script = f"""
try {{
  $mgr = New-Object -ComObject HNetCfg.HNetShare
  foreach ($conn in $mgr.EnumEveryConnection) {{
    $cfg = $mgr.INetSharingConfigurationForINetConnection.Invoke($conn)
    if ($cfg.SharingEnabled) {{
      $cfg.DisableSharing()
    }}
  }}
}} catch {{}}
{stop_svc}
Write-Output "CLEAR_ICS_OK"
"""
    res = runner(script, 30)
    log_file = _append_bom_log(
        paths,
        "clear-ics",
        [
            f"ics_active={ics_active}, ics_on_53_67={ics_on_53_67}",
            f"returncode={res.returncode}",
            f"stderr={res.stderr.strip()}",
        ],
    )
    if res.returncode != 0:
        return ActionResult(
            ok=False,
            code="CLEAR_ICS_FAILED",
            message="清理系统 ICS 共享失败",
            exit_code=1,
            details={"stderr": res.stderr.strip()},
            log_file=log_file,
        )
    return ActionResult(
        ok=True,
        code="ICS_CLEARED",
        message="已关闭系统 ICS 共享（未改动第三方 DNS 服务）",
        exit_code=0,
        log_file=log_file,
    )


def _clear_netnat(
    paths: AppPaths,
    cfg: AppConfig,
    force: bool,
    runner: Callable[[str, int], PsResult],
    snapshot_fn: Callable[..., dict[str, Any]],
) -> ActionResult:
    snapshot = snapshot_fn(cfg, probe_network=False)
    foreign = [
        str(n.get("name", ""))
        for n in (snapshot.get("net_nats") or [])
        if str(n.get("name", "")) and str(n.get("name", "")) != NAT_NAME
    ]
    if not foreign:
        return ActionResult(
            ok=True,
            code="NO_FOREIGN_NAT",
            message="当前无其他 NetNat 冲突",
            exit_code=0,
        )

    if not force:
        log_file = _append_bom_log(
            paths, "clear-netnat", [f"REFUSED without --force; foreign={foreign}"]
        )
        return ActionResult(
            ok=False,
            code="CONFIRM_REQUIRED",
            message=f"发现外部 NAT ({', '.join(foreign)})，请确认后再强制清理",
            exit_code=2,
            details={"nat_names": foreign},
            log_file=log_file,
        )

    rm_lines = "\n".join(
        f"Remove-NetNat -Name {_ps_quote(name)} -Confirm:$false -ErrorAction Stop" for name in foreign
    )
    script = rm_lines + '\nWrite-Output "CLEAR_NETNAT_OK"\n'
    res = runner(script, 30)
    log_file = _append_bom_log(
        paths,
        "clear-netnat",
        [f"removed={foreign}", f"returncode={res.returncode}", f"stderr={res.stderr.strip()}"],
    )
    if res.returncode != 0:
        return ActionResult(
            ok=False,
            code="CLEAR_NETNAT_FAILED",
            message="清理外部 NetNat 失败",
            exit_code=1,
            details={"nat_names": foreign, "stderr": res.stderr.strip()},
            log_file=log_file,
        )
    return ActionResult(
        ok=True,
        code="NETNAT_CLEARED",
        message=f"已清理外部 NAT: {', '.join(foreign)}",
        exit_code=0,
        details={"nat_names": foreign},
        log_file=log_file,
    )


def execute_action(
    action: str,
    paths: AppPaths,
    cfg: AppConfig,
    *,
    force: bool = False,
    ext_id: str | None = None,
    runner: Callable[[str, int], PsResult] = run_powershell,
    snapshot_fn: Callable[..., dict[str, Any]] = collect_system_snapshot,
) -> ActionResult:
    """Execute a WireDeck system action (assumes caller already verified privilege boundary)."""
    if action not in SUPPORTED_ACTIONS:
        return ActionResult(
            ok=False,
            code="UNKNOWN_ACTION",
            message=f"不支持的操作: {action}",
            exit_code=1,
        )

    if action == "enable-direct":
        return _enable_direct(paths, cfg, runner, snapshot_fn)
    if action == "disable-direct":
        return _disable_direct(paths, cfg, runner, snapshot_fn)
    if action == "reset-nic":
        return _reset_nic(paths, cfg, runner, snapshot_fn)
    if action == "fix-nic":
        return _fix_nic(paths, cfg, runner, snapshot_fn)
    if action == "fix-uwp":
        return _fix_uwp(paths, cfg, runner)
    if action == "clear-ics":
        return _clear_ics(paths, cfg, runner, snapshot_fn)
    if action == "clear-netnat":
        return _clear_netnat(paths, cfg, force, runner, snapshot_fn)
    if action == "run-ext":
        if not ext_id:
            return ActionResult(
                ok=False, code="MISSING_EXT_ID", message="缺少扩展 ID", exit_code=1
            )
        from wiredeck.extensions import run_extension_by_id

        er = run_extension_by_id(paths, ext_id, mode="fix")
        ok = er.status in (Severity.OK, Severity.IDLE)
        log_file = _append_bom_log(
            paths,
            f"run-ext:{ext_id}",
            [f"status={er.status.value}, code={er.code}, msg={er.message}"],
        )
        return ActionResult(
            ok=ok,
            code=er.code,
            message=er.message,
            exit_code=0 if ok else 1,
            details=er.to_dict(),
            log_file=log_file,
        )

    return ActionResult(ok=False, code="UNKNOWN_ACTION", message=action, exit_code=1)
