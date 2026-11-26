"""6-layer dynamic fault classifier and AI extension merger."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

from wiredeck.config import AppConfig
from wiredeck.models import (
    SEVERITY_RANK,
    DiagnosticReport,
    ExtensionRunResult,
    FaultItem,
    LayerReport,
    Severity,
)
from wiredeck.paths import AppPaths
from wiredeck.probes import collect_system_snapshot
from wiredeck.version import APP_VERSION, FW_RULE_NAT, NAT_NAME, SCHEMA_VERSION

TUN_KEYWORD_RE = re.compile(
    r"(sing-tun|wintun|clash|mihomo|wireguard|openvpn|lightxtreme|tap-windows|"
    r"nekobox|v2ray|shadowsocks|tailscale|\btun\b|tunnel)",
    re.IGNORECASE,
)

HW_ERROR_WARN_THRESHOLD = 50


TETHER_HOTSPOT_RE = re.compile(
    r"(rndis|remote\s*ndis|mobile\s*broadband|apple\s*mobile\s*device|cellular|wwan|usb\s*tether)",
    re.IGNORECASE,
)


def select_ethernet_adapter(
    net_adapters: list[dict[str, Any]],
    preferred_name: str = "",
    default_routes: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Dynamically pick the primary physical wired Ethernet adapter (avoiding USB hotspots)."""
    physical = [
        a
        for a in net_adapters
        if not a.get("is_virtual", False) and not a.get("is_wireless", False)
    ]
    if not physical:
        return None
    if preferred_name:
        for a in physical:
            if a.get("name") == preferred_name:
                return a

    uplink_aliases = {
        str(r.get("interface_alias", ""))
        for r in (default_routes or [])
        if not r.get("is_virtual_or_tun", False)
    }

    def _is_tether_or_uplink(a: dict[str, Any]) -> bool:
        desc = str(a.get("interface_description", ""))
        name = str(a.get("name", ""))
        return bool(TETHER_HOTSPOT_RE.search(desc)) or (name in uplink_aliases)

    # Prefer dedicated wired Ethernet NICs over USB cellular/RNDIS tethering uplinks
    dedicated = [a for a in physical if not _is_tether_or_uplink(a)]
    candidates = dedicated if dedicated else physical

    def _score(a: dict[str, Any]) -> tuple[int, int, int]:
        status = str(a.get("status", "")).lower()
        is_up = 2 if status == "up" else (1 if status != "disabled" else 0)
        speed = int(a.get("link_speed_bps") or 0)
        not_disabled = 1 if status != "disabled" else 0
        return (is_up, speed, not_disabled)

    return max(candidates, key=_score)


def select_uplink_adapter(
    net_adapters: list[dict[str, Any]],
    default_routes: list[dict[str, Any]],
    eth_name: str,
    preferred_name: str = "",
) -> dict[str, Any] | None:
    """Dynamically pick the physical upstream internet adapter (typically WLAN)."""
    if preferred_name:
        for a in net_adapters:
            if a.get("name") == preferred_name:
                return a

    by_name = {str(a.get("name", "")): a for a in net_adapters}
    non_tun_routes = [
        r
        for r in default_routes
        if not r.get("is_virtual_or_tun", False)
        and str(r.get("interface_alias", "")) != eth_name
    ]
    non_tun_routes.sort(key=lambda r: int(r.get("metric", 9999)))
    for r in non_tun_routes:
        alias = str(r.get("interface_alias", ""))
        if alias in by_name:
            return by_name[alias]

    # Fallback: pick an Up wireless adapter, or any other Up physical adapter
    for a in net_adapters:
        if (
            not a.get("is_virtual", False)
            and a.get("name") != eth_name
            and str(a.get("status", "")).lower() == "up"
        ):
            return a
    return None


def _is_tun_route(route: dict[str, Any]) -> bool:
    if route.get("is_virtual_or_tun"):
        return True
    desc = f"{route.get('interface_alias', '')} {route.get('interface_description', '')}"
    return bool(TUN_KEYWORD_RE.search(desc))


def _format_speed_short(bps: int, fallback: str) -> str:
    if bps >= 1_000_000_000:
        gbps = bps / 1_000_000_000
        return f"{gbps:g} Gbps"
    if bps >= 1_000_000:
        return f"{bps // 1_000_000} Mbps"
    return fallback or "未连接"


def _layer_status_from_faults(faults: list[FaultItem], default: Severity) -> Severity:
    if not faults:
        return default
    return max((f.severity for f in faults), key=lambda s: SEVERITY_RANK[s])


def classify_snapshot(
    snapshot: dict[str, Any],
    cfg: AppConfig,
    ext_results: list[ExtensionRunResult] | None = None,
) -> DiagnosticReport:
    """Classify a system snapshot into the 6-layer WireDeck diagnostic report."""
    net_adapters = list(snapshot.get("net_adapters") or [])
    pnp_devices = list(snapshot.get("pnp_ethernet_devices") or [])
    ipv4_addrs = list(snapshot.get("ipv4_addresses") or [])
    default_routes = list(snapshot.get("default_routes") or [])
    ip_forwarding = dict(snapshot.get("ip_forwarding") or {})
    ics_ifaces = list(snapshot.get("ics_sharing_interfaces") or [])
    udp_listeners = list(snapshot.get("udp_listeners") or [])
    net_nats = list(snapshot.get("net_nats") or [])
    fw_rules = dict(snapshot.get("firewall_rules") or {})
    uwp_missing = list(snapshot.get("uwp_missing_packages") or [])
    stream_clients = dict(snapshot.get("stream_clients") or {})
    connectivity = dict(snapshot.get("connectivity") or {})

    eth = select_ethernet_adapter(net_adapters, cfg.ethernet_interface, default_routes=default_routes)
    eth_name = str(eth.get("name", "")) if eth else ""
    uplink = select_uplink_adapter(net_adapters, default_routes, eth_name, cfg.uplink_interface)
    uplink_name = str(uplink.get("name", "")) if uplink else ""

    # ---------------- L1: NIC Hardware ----------------
    l1_faults: list[FaultItem] = []
    l1_metrics: dict[str, Any] = {
        "selected_ethernet": eth_name,
        "selected_uplink": uplink_name,
        "physical_ethernets": [
            a.get("name")
            for a in net_adapters
            if not a.get("is_virtual") and not a.get("is_wireless")
        ],
    }

    if eth is None:
        pcie_disabled = [d for d in pnp_devices if int(d.get("parent_problem_code") or 0) == 22]
        nic_disabled = [d for d in pnp_devices if int(d.get("problem_code") or 0) == 22]
        if pcie_disabled:
            dev = pcie_disabled[0]
            l1_faults.append(
                FaultItem(
                    code="NIC_PCIE_PARENT_DISABLED",
                    layer="L1",
                    severity=Severity.FAIL,
                    title="PCIe 端口被禁用",
                    message="有线网卡上级 PCIe 通道已禁用，点一键唤醒",
                    fix_action="fix-nic",
                    details={
                        "device": dev.get("friendly_name"),
                        "parent": dev.get("parent_friendly_name"),
                        "parent_instance_id": dev.get("parent_instance_id"),
                    },
                )
            )
            l1_summary = "PCIe 禁用"
        elif nic_disabled:
            dev = nic_disabled[0]
            l1_faults.append(
                FaultItem(
                    code="NIC_DISABLED",
                    layer="L1",
                    severity=Severity.FAIL,
                    title="有线网卡已禁用",
                    message="有线网卡处于禁用状态，点一键启用",
                    fix_action="fix-nic",
                    details={"device": dev.get("friendly_name"), "instance_id": dev.get("instance_id")},
                )
            )
            l1_summary = "已禁用"
        else:
            l1_faults.append(
                FaultItem(
                    code="NIC_NOT_FOUND",
                    layer="L1",
                    severity=Severity.FAIL,
                    title="未检测到有线网卡",
                    message="请检查网口硬件或插好 USB 有线网卡",
                )
            )
            l1_summary = "未检测到"
    else:
        status_lower = str(eth.get("status", "")).lower()
        if status_lower == "disabled":
            l1_faults.append(
                FaultItem(
                    code="NIC_DISABLED",
                    layer="L1",
                    severity=Severity.FAIL,
                    title="有线网卡已禁用",
                    message="有线网卡处于禁用状态，点一键启用",
                    fix_action="fix-nic",
                    details={"adapter": eth_name},
                )
            )
            l1_summary = "已禁用"
        else:
            ps_flags = list(eth.get("powersave_enabled") or [])
            if ps_flags:
                l1_faults.append(
                    FaultItem(
                        code="NIC_POWERSAVE_ENABLED",
                        layer="L1",
                        severity=Severity.WARN,
                        title="网卡节能未关闭",
                        message="EEE/节能项可能导致串流微卡顿，建议一键关闭",
                        fix_action="fix-nic",
                        details={"adapter": eth_name, "flags": ps_flags},
                    )
                )
                l1_summary = "节能待关"
            else:
                l1_summary = "正常"

    l1_base_status = Severity.OK
    l1 = LayerReport(
        layer="L1",
        name="网卡",
        status=_layer_status_from_faults(l1_faults, l1_base_status),
        summary=l1_summary,
        faults=l1_faults,
        metrics=l1_metrics,
    )

    # ---------------- L2: Physical Cable ----------------
    l2_faults: list[FaultItem] = []
    l2_metrics: dict[str, Any] = {}
    if l1.status == Severity.FAIL or eth is None:
        l2_base_status = Severity.IDLE
        l2_summary = "待检测"
    else:
        status_lower = str(eth.get("status", "")).lower()
        bps = int(eth.get("link_speed_bps") or 0)
        l2_metrics["link_speed_bps"] = bps
        hw_errs = (
            int(eth.get("rx_errors") or 0)
            + int(eth.get("tx_errors") or 0)
            + int(eth.get("rx_discards") or 0)
            + int(eth.get("tx_discards") or 0)
        )
        l2_metrics["hw_errors"] = hw_errs

        if status_lower != "up":
            l2_faults.append(
                FaultItem(
                    code="CABLE_UNPLUGGED",
                    layer="L2",
                    severity=Severity.FAIL,
                    title="网线未接通",
                    message="请将网线两端插紧笔记本与游戏机并开机",
                    details={"adapter": eth_name, "status": eth.get("status")},
                )
            )
            l2_base_status = Severity.OK
            l2_summary = "未插线"
        else:
            l2_base_status = Severity.OK
            l2_summary = _format_speed_short(bps, str(eth.get("link_speed_text", "")))
            if 0 < bps < 1_000_000_000:
                l2_faults.append(
                    FaultItem(
                        code="LINK_DEGRADED_100M",
                        layer="L2",
                        severity=Severity.WARN,
                        title="网线仅跑百兆",
                        message="当前协商 100Mbps，建议更换超五类/六类千兆网线",
                        details={"link_speed_bps": bps},
                    )
                )
            if hw_errs > HW_ERROR_WARN_THRESHOLD:
                l2_faults.append(
                    FaultItem(
                        code="NIC_HW_ERRORS",
                        layer="L2",
                        severity=Severity.WARN,
                        title="链路硬件错包",
                        message="检测到网线错包偏高，请重插或更换网线",
                        details={"hw_errors": hw_errs},
                    )
                )

    l2 = LayerReport(
        layer="L2",
        name="网线",
        status=_layer_status_from_faults(l2_faults, l2_base_status),
        summary=l2_summary,
        faults=l2_faults,
        metrics=l2_metrics,
    )

    # ---------------- L3: Conflict Gate ----------------
    l3_faults: list[FaultItem] = []
    l3_metrics: dict[str, Any] = {}

    tun_routes = [r for r in default_routes if _is_tun_route(r)]
    if tun_routes:
        tun_names = sorted({str(r.get("interface_alias") or r.get("interface_description") or "TUN") for r in tun_routes})
        l3_metrics["tun_routes"] = tun_names
        l3_faults.append(
            FaultItem(
                code="VPN_TUN_ACTIVE",
                layer="L3",
                severity=Severity.FAIL,
                title="VPN 正在接管网络",
                message="请先断开 VPN，再点「开启游戏直连」",
                fix_action=None,
                details={"tun_interfaces": tun_names},
            )
        )

    if ics_ifaces:
        l3_metrics["ics_interfaces"] = [i.get("name") for i in ics_ifaces]
        l3_faults.append(
            FaultItem(
                code="ICS_ACTIVE",
                layer="L3",
                severity=Severity.FAIL,
                title="系统 ICS 共享冲突",
                message="检测到 Windows ICS 开启，点一键清理防冲突",
                fix_action="clear-ics",
                details={"interfaces": ics_ifaces},
            )
        )

    port67 = [u for u in udp_listeners if int(u.get("port") or 0) == 67]
    port53 = [u for u in udp_listeners if int(u.get("port") or 0) == 53]
    ics_on_53 = [
        u
        for u in port53
        if "sharedaccess" in str(u.get("service_name", "")).lower()
    ]
    non_ics_on_53 = [
        u
        for u in port53
        if "sharedaccess" not in str(u.get("service_name", "")).lower()
    ]

    if ics_on_53 or port67:
        l3_faults.append(
            FaultItem(
                code="PORT_53_67_OCCUPIED",
                layer="L3",
                severity=Severity.FAIL,
                title="53/67 端口被占用",
                message="ICS/DHCP 端口冲突，请清理 ICS 或关闭占用程序",
                fix_action="clear-ics" if ics_on_53 else None,
                details={"listeners": ics_on_53 + port67},
            )
        )
    elif non_ics_on_53 and not ics_ifaces:
        owner = str(non_ics_on_53[0].get("process_name") or "本地DNS")
        l3_faults.append(
            FaultItem(
                code="PORT_53_NON_ICS_WARNING",
                layer="L3",
                severity=Severity.WARN,
                title="本机 DNS 占 53 端口",
                message=f"{owner} 占用 53 端口，不影响直连，仅作提示",
                fix_action=None,
                details={"listeners": non_ics_on_53},
            )
        )

    foreign_nats = [n for n in net_nats if str(n.get("name", "")) != NAT_NAME]
    if foreign_nats:
        nat_names = [str(n.get("name", "")) for n in foreign_nats]
        l3_metrics["foreign_nats"] = nat_names
        first_nat = nat_names[0]
        l3_faults.append(
            FaultItem(
                code="FOREIGN_NETNAT_EXISTS",
                layer="L3",
                severity=Severity.FAIL,
                title="存在其他 NAT 规则",
                message=f"已有 {first_nat}，需确认清理后再开启直连",
                fix_action="clear-netnat",
                details={"nat_names": nat_names, "nats": foreign_nats},
            )
        )

    if any(f.code == "VPN_TUN_ACTIVE" for f in l3_faults):
        l3_summary = "VPN 在线"
    elif any(f.severity == Severity.FAIL for f in l3_faults):
        l3_summary = "存在冲突"
    elif any(f.severity == Severity.WARN for f in l3_faults):
        l3_summary = "有提醒"
    else:
        l3_summary = "通过"

    l3 = LayerReport(
        layer="L3",
        name="门禁",
        status=_layer_status_from_faults(l3_faults, Severity.OK),
        summary=l3_summary,
        faults=l3_faults,
        metrics=l3_metrics,
    )

    # ---------------- L4: WinNAT Sharing ----------------
    l4_faults: list[FaultItem] = []
    wiredeck_nat = next((n for n in net_nats if str(n.get("name", "")) == NAT_NAME), None)
    eth_ips = [ip for ip in ipv4_addrs if str(ip.get("interface_alias", "")) == eth_name]
    has_expected_ip = any(
        str(ip.get("ip")) == cfg.host_ip and int(ip.get("prefix_length") or 0) == cfg.prefix_length
        for ip in eth_ips
    )
    eth_has_gw = any(str(r.get("interface_alias", "")) == eth_name for r in default_routes)
    eth_fwd = bool(ip_forwarding.get(eth_name, False)) if eth_name else False
    uplink_fwd = bool(ip_forwarding.get(uplink_name, False)) if uplink_name else False
    fw_nat_ok = bool(fw_rules.get(FW_RULE_NAT, False))

    l4_metrics = {
        "nat_active": wiredeck_nat is not None,
        "has_expected_ip": has_expected_ip,
        "eth_forwarding": eth_fwd,
        "uplink_forwarding": uplink_fwd,
        "fw_nat_ok": fw_nat_ok,
    }

    if eth_has_gw:
        l4_faults.append(
            FaultItem(
                code="ETH_HAS_UNEXPECTED_GATEWAY",
                layer="L4",
                severity=Severity.WARN,
                title="有线口存在默认网关",
                message="有线口不应配网关，重新开启直连可自动修正",
                fix_action="enable-direct",
                details={"adapter": eth_name},
            )
        )

    direct_active = False
    if wiredeck_nat is not None:
        if not has_expected_ip:
            l4_faults.append(
                FaultItem(
                    code="ETH_IP_MISSING",
                    layer="L4",
                    severity=Severity.FAIL,
                    title="有线静态 IP 缺失",
                    message=f"有线口未配置 {cfg.host_ip}，点一键修复",
                    fix_action="enable-direct",
                )
            )
        if not (eth_fwd and (uplink_fwd or not uplink_name)):
            l4_faults.append(
                FaultItem(
                    code="FORWARDING_DISABLED",
                    layer="L4",
                    severity=Severity.FAIL,
                    title="网卡转发未开启",
                    message="IP Forwarding 未开启，点一键修复共享",
                    fix_action="enable-direct",
                )
            )
        if not fw_nat_ok:
            l4_faults.append(
                FaultItem(
                    code="FW_RULE_MISSING",
                    layer="L4",
                    severity=Severity.WARN,
                    title="防火墙放行缺失",
                    message="未检测到 WireDeck 放行规则，点一键补齐",
                    fix_action="enable-direct",
                )
            )
        l4_base_status = Severity.OK
        if not any(f.severity == Severity.FAIL for f in l4_faults):
            direct_active = True
            l4_summary = "已开启"
        else:
            l4_summary = "异常"
    else:
        l4_base_status = Severity.IDLE
        l4_summary = "未开启"

    l4 = LayerReport(
        layer="L4",
        name="共享",
        status=_layer_status_from_faults(l4_faults, l4_base_status),
        summary=l4_summary,
        faults=l4_faults,
        metrics=l4_metrics,
    )

    # ---------------- L5: Console & Uplink ----------------
    l5_faults: list[FaultItem] = []
    l5_metrics: dict[str, Any] = {
        "peer_ip": cfg.peer_ip,
        "peer_rtt_ms": connectivity.get("peer_rtt_ms"),
        "host_internet_ok": connectivity.get("host_internet_ok"),
        "xbox_live_ok": connectivity.get("xbox_live_ok") if cfg.console_profile == "xbox" else "skipped",
    }

    if l1.status == Severity.FAIL or l2.status == Severity.FAIL:
        l5_base_status = Severity.IDLE
        l5_summary = "待连线"
    else:
        peer_ok = connectivity.get("peer_reachable")
        inet_ok = connectivity.get("host_internet_ok")
        xbox_ok = connectivity.get("xbox_live_ok")

        if peer_ok is False:
            l5_faults.append(
                FaultItem(
                    code="PEER_UNREACHABLE",
                    layer="L5",
                    severity=Severity.FAIL if direct_active else Severity.IDLE,
                    title="未连上游戏机",
                    message=f"请确认主机已开机并设为静态 IP {cfg.peer_ip}",
                    details={"peer_ip": cfg.peer_ip},
                )
            )
        if inet_ok is False:
            l5_faults.append(
                FaultItem(
                    code="HOST_INTERNET_DOWN",
                    layer="L5",
                    severity=Severity.FAIL,
                    title="笔记本外网不通",
                    message="请检查 Wi-Fi/热点连接是否正常上网",
                )
            )
        if cfg.console_profile == "xbox" and xbox_ok is False:
            l5_faults.append(
                FaultItem(
                    code="XBOX_LIVE_UNREACHABLE",
                    layer="L5",
                    severity=Severity.WARN,
                    title="Xbox 服务响应异常",
                    message="Xbox 认证接口暂不可达，请稍后复测或换网络",
                    details={"status": connectivity.get("xbox_live_status")},
                )
            )

        if inet_ok is False:
            l5_summary = "外网不通"
            l5_base_status = Severity.OK
        elif peer_ok is True:
            rtt = connectivity.get("peer_rtt_ms")
            l5_summary = f"{rtt:.1f} ms" if isinstance(rtt, (int, float)) else "已连通"
            l5_base_status = Severity.OK
        elif peer_ok is None:
            l5_summary = "未探测"
            l5_base_status = Severity.IDLE
        else:
            l5_summary = "未开机/未配IP"
            l5_base_status = Severity.IDLE

    # Filter out IDLE faults from raising status to WARN/FAIL
    active_l5_faults = [f for f in l5_faults if f.severity in (Severity.WARN, Severity.FAIL)]
    l5 = LayerReport(
        layer="L5",
        name="主机",
        status=_layer_status_from_faults(active_l5_faults, l5_base_status),
        summary=l5_summary,
        faults=l5_faults,
        metrics=l5_metrics,
    )

    # ---------------- L6: Streaming Readiness ----------------
    l6_faults: list[FaultItem] = []
    l6_metrics: dict[str, Any] = {
        "xstreaming_path": stream_clients.get("xstreaming_path"),
        "xbox_app_installed": bool(stream_clients.get("xbox_app_installed", False)),
        "uwp_missing_count": len(uwp_missing),
    }

    if cfg.console_profile != "xbox":
        l6_base_status = Severity.SKIPPED
        l6_summary = "轻量模式"
        l6_metrics["skipped_reason"] = f"profile={cfg.console_profile}"
    else:
        l6_base_status = Severity.OK
        if uwp_missing:
            l6_faults.append(
                FaultItem(
                    code="UWP_LOOPBACK_MISSING",
                    layer="L6",
                    severity=Severity.WARN,
                    title="商店/Xbox 未解除隔离",
                    message="开代理时商店与 Xbox 会打不开，点一键豁免",
                    fix_action="fix-uwp",
                    details={"missing_packages": uwp_missing},
                )
            )
        if not stream_clients.get("xstreaming_path") and not stream_clients.get("xbox_app_installed"):
            l6_faults.append(
                FaultItem(
                    code="STREAM_CLIENT_MISSING",
                    layer="L6",
                    severity=Severity.WARN,
                    title="未装串流客户端",
                    message="建议一键下载官方 XStreaming 开始串流",
                    fix_action="download-stream",
                )
            )
        l6_summary = "已就绪" if not l6_faults else "待准备"

    l6 = LayerReport(
        layer="L6",
        name="串流",
        status=_layer_status_from_faults(l6_faults, l6_base_status),
        summary=l6_summary,
        faults=l6_faults,
        metrics=l6_metrics,
    )

    layers = [l1, l2, l3, l4, l5, l6]
    base_statuses = {
        "L1": l1_base_status,
        "L2": l2_base_status,
        "L3": Severity.OK,
        "L4": l4_base_status,
        "L5": l5_base_status,
        "L6": l6_base_status,
    }

    # ---------------- Merge AI / DSH Extensions ----------------
    serialized_exts: list[dict[str, Any]] = []
    if ext_results:
        layer_map = {layer.layer: layer for layer in layers}
        for er in ext_results:
            serialized_exts.append(er.to_dict())
            target = layer_map.get(er.layer) or l1
            if er.code == "EXT_ERROR":
                target.faults.append(
                    FaultItem(
                        code="EXT_ERROR",
                        layer=target.layer,
                        severity=Severity.WARN,
                        title=f"扩展异常: {er.name[:8]}",
                        message=er.message[:36] or "扩展执行超时或输出异常",
                        source="extension",
                        fix_action=None,
                        ext_id=er.ext_id,
                        details=er.details,
                    )
                )
            else:
                overridden: list[str] = []
                if er.overrides_faults:
                    override_set = set(er.overrides_faults)
                    remaining: list[FaultItem] = []
                    for f in target.faults:
                        if f.source == "builtin" and f.code in override_set:
                            overridden.append(f.code)
                        else:
                            remaining.append(f)
                    target.faults = remaining

                if er.status in (Severity.WARN, Severity.FAIL):
                    target.faults.append(
                        FaultItem(
                            code=er.code,
                            layer=target.layer,
                            severity=er.status,
                            title=er.title[:16] or er.name[:12],
                            message=er.message[:40],
                            source="extension",
                            fix_action="run-ext" if er.fix_available else None,
                            ext_id=er.ext_id,
                            overridden_from=overridden,
                            details=er.details,
                        )
                    )
            active_faults = [f for f in target.faults if f.severity in (Severity.WARN, Severity.FAIL)]
            target.status = _layer_status_from_faults(active_faults, base_statuses[target.layer])
            if not active_faults and target.status == Severity.OK:
                healthy_defaults = {
                    "L1": "正常",
                    "L2": l2_summary,
                    "L3": "通过",
                    "L4": "已开启" if direct_active else "未开启",
                    "L5": l5_summary,
                    "L6": "已就绪",
                }
                target.summary = healthy_defaults.get(target.layer, "正常")
            elif er.status == Severity.FAIL and target.status == Severity.FAIL:
                target.summary = "扩展拦截"
            elif er.status == Severity.WARN and target.status == Severity.WARN:
                target.summary = "扩展提醒"

    # ---------------- Final Summary & Mode ----------------
    can_enable_direct = (l3.status != Severity.FAIL) and (l1.status != Severity.FAIL)
    if l3.status == Severity.FAIL:
        mode = "blocked"
    elif direct_active:
        mode = "direct"
    else:
        mode = "normal"

    # Pick highest-severity fault (FAIL first, then WARN) across L1 -> L6
    primary_fault: FaultItem | None = None
    for sev in (Severity.FAIL, Severity.WARN):
        for layer in layers:
            for f in layer.faults:
                if f.severity == sev:
                    primary_fault = f
                    break
            if primary_fault is not None:
                break
        if primary_fault is not None:
            break

    if primary_fault is not None:
        headline = f"检测到断点：{primary_fault.title}"
    elif mode == "direct":
        headline = "千兆直连运行中，随时开玩"
    else:
        headline = "环境已就绪，可一键开启游戏直连"

    return DiagnosticReport(
        schema_version=SCHEMA_VERSION,
        app_version=APP_VERSION,
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
        console_profile=cfg.console_profile,
        mode=mode,
        can_enable_direct=can_enable_direct,
        headline=headline,
        primary_fault=primary_fault,
        layers=layers,
        snapshot=snapshot,
        extensions=serialized_exts,
    )


def run_diagnosis(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    include_extensions: bool = True,
    probe_network: bool = True,
    snapshot_fn: Callable[..., dict[str, Any]] = collect_system_snapshot,
) -> DiagnosticReport:
    """Collect a fresh system snapshot, optionally run auto_check extensions, and classify."""
    snapshot = snapshot_fn(cfg, probe_network=probe_network)
    ext_results: list[ExtensionRunResult] = []
    if include_extensions:
        try:
            from wiredeck.extensions import run_auto_check_extensions

            ext_results = run_auto_check_extensions(paths)
        except ImportError:
            ext_results = []
    return classify_snapshot(snapshot, cfg, ext_results=ext_results)
