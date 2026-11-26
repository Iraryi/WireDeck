"""6-layer wireless fault classifier for 5 GHz Mobile Hotspot & Same-LAN Wi-Fi streaming."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from wiredeck.config import AppConfig
from wiredeck.diagnostics import _layer_status_from_faults
from wiredeck.models import (
    SEVERITY_RANK,
    DiagnosticReport,
    ExtensionRunResult,
    FaultItem,
    LayerReport,
    Severity,
)
from wiredeck.paths import AppPaths
from wiredeck.presets import get_preset
from wiredeck.version import APP_VERSION, SCHEMA_VERSION
from wiredeck.wireless_probes import collect_wireless_snapshot

TUN_KEYWORD_RE = re.compile(
    r"(sing-tun|wintun|clash|mihomo|wireguard|openvpn|lightxtreme|tap-windows|"
    r"nekobox|v2ray|shadowsocks|tailscale|\btun\b|tunnel)",
    re.IGNORECASE,
)


def select_wifi_adapter(
    wifi_adapters: list[dict[str, Any]],
    preferred_name: str = "",
) -> dict[str, Any] | None:
    if not wifi_adapters:
        return None
    if preferred_name:
        for a in wifi_adapters:
            if a.get("name") == preferred_name:
                return a

    def _score(a: dict[str, Any]) -> tuple[int, int]:
        st = str(a.get("status", "")).lower()
        up = 2 if st == "up" else (1 if st != "disabled" else 0)
        return (up, int(a.get("link_speed_bps") or 0))

    return max(wifi_adapters, key=_score)


def _same_subnet(ip_a: str, prefix: int, ip_b: str) -> bool:
    try:
        net = ipaddress.IPv4Interface(f"{ip_a}/{prefix}").network
        return ipaddress.IPv4Address(ip_b) in net
    except Exception:
        return False


def classify_wireless_snapshot(
    snapshot: dict[str, Any],
    cfg: AppConfig,
    ext_results: list[ExtensionRunResult] | None = None,
) -> DiagnosticReport:
    """Classify wireless hardware, RF band, hotspot/LAN reachability, anti-stutter & preset readiness."""
    preset = get_preset(cfg.active_preset)
    submode = cfg.wireless_submode if cfg.wireless_submode in ("hotspot", "lan") else "hotspot"

    wifi_adapters = snapshot.get("wifi_adapters") or []
    wifi = select_wifi_adapter(wifi_adapters, cfg.wifi_interface)
    wifi_name = str(wifi["name"]) if wifi else ""
    wlan = snapshot.get("wlan") or {}
    autoconfig_enabled = snapshot.get("autoconfig_enabled")
    ipv4_addresses = snapshot.get("ipv4_addresses") or []
    default_routes = snapshot.get("default_routes") or []
    wired_nat_active = bool(snapshot.get("wired_nat_active", False))
    hotspot = snapshot.get("hotspot") or {}
    hotspot_clients = snapshot.get("hotspot_clients") or []
    uwp_missing = snapshot.get("uwp_missing_packages") or []
    stream_clients = snapshot.get("stream_clients") or {}
    connectivity = snapshot.get("connectivity") or {}

    wifi_ip = ""
    wifi_prefix = 24
    for ip_row in ipv4_addresses:
        if ip_row.get("interface_alias") == wifi_name:
            wifi_ip = str(ip_row.get("ip", ""))
            wifi_prefix = int(ip_row.get("prefix_length") or 24)
            break

    # ---------------- W1: Wi-Fi Adapter Hardware & Anti-Stutter Flags ----------------
    w1_faults: list[FaultItem] = []
    w1_metrics: dict[str, Any] = {
        "selected_wifi": wifi_name or None,
        "wifi_adapters": [str(a.get("name", "")) for a in wifi_adapters],
        "powersave_keywords": [],
    }
    if wifi is None:
        w1_base = Severity.FAIL
        w1_summary = "未检测到"
        w1_faults.append(
            FaultItem(
                code="WIFI_NOT_FOUND",
                layer="W1",
                severity=Severity.FAIL,
                title="未检测到无线网卡",
                message="请确认电脑无线网卡未被关闭或插好 USB Wi-Fi 网卡",
            )
        )
    else:
        st = str(wifi.get("status", "")).lower()
        ps_flags = list(wifi.get("powersave_enabled") or [])
        w1_metrics["description"] = wifi.get("interface_description", "")
        w1_metrics["status"] = wifi.get("status", "")
        w1_metrics["powersave_keywords"] = ps_flags
        if st == "disabled":
            w1_base = Severity.FAIL
            w1_summary = "已禁用"
            w1_faults.append(
                FaultItem(
                    code="WIFI_DISABLED",
                    layer="W1",
                    severity=Severity.FAIL,
                    title="无线网卡已禁用",
                    message=f"请在系统设置中启用 {wifi_name}，或打开飞行模式/Wi-Fi 开关",
                )
            )
        else:
            w1_base = Severity.OK
            w1_summary = f"{wifi.get('interface_description') or wifi_name}"
            if ps_flags:
                w1_faults.append(
                    FaultItem(
                        code="WIFI_POWERSAVE_ENABLED",
                        layer="W1",
                        severity=Severity.WARN,
                        title="无线节能/合并已开启",
                        message="检测到 MIMO 节能或数据包合并，可能造成串流周期抖动，建议开启无线防抖",
                        fix_action="optimize-wifi",
                        details={"adapter": wifi_name, "flags": ps_flags},
                    )
                )

    w1 = LayerReport(
        layer="W1",
        name="无线网卡",
        status=_layer_status_from_faults(w1_faults, w1_base),
        summary=w1_summary,
        faults=w1_faults,
        metrics=w1_metrics,
    )

    # ---------------- W2: RF Band, Signal & PHY Link Rate ----------------
    w2_faults: list[FaultItem] = []
    connected = bool(wlan.get("connected", False))
    band_ghz = wlan.get("band_ghz")
    signal_pct = wlan.get("signal_pct")
    rssi_dbm = wlan.get("rssi_dbm")
    rx_mbps = wlan.get("rx_rate_mbps")
    tx_mbps = wlan.get("tx_rate_mbps")
    w2_metrics: dict[str, Any] = {
        "connected": connected,
        "ssid": wlan.get("ssid", ""),
        "band": wlan.get("band", ""),
        "band_ghz": band_ghz,
        "channel": wlan.get("channel"),
        "radio_type": wlan.get("radio_type", ""),
        "signal_pct": signal_pct,
        "rssi_dbm": rssi_dbm,
        "rx_rate_mbps": rx_mbps,
        "tx_rate_mbps": tx_mbps,
        "wifi_ip": wifi_ip,
    }

    if w1.status == Severity.FAIL:
        w2_base = Severity.IDLE
        w2_summary = "待网卡就绪"
    elif not connected:
        if submode == "lan":
            w2_base = Severity.FAIL
            w2_summary = "Wi-Fi 未连接"
            w2_faults.append(
                FaultItem(
                    code="WIFI_DISCONNECTED",
                    layer="W2",
                    severity=Severity.FAIL,
                    title="Wi-Fi 未连接",
                    message="同 Wi-Fi 模式需要电脑与设备连入同一路由器/热点",
                )
            )
        else:
            # Hotspot mode can still share an Ethernet uplink even when Wi-Fi STA is disconnected
            w2_base = Severity.IDLE
            w2_summary = "未连上游 Wi-Fi"
    else:
        w2_base = Severity.OK
        band_txt = wlan.get("band") or ("5 GHz" if band_ghz == 5.0 else ("6 GHz" if band_ghz == 6.0 else "2.4 GHz"))
        ch_txt = f" Ch{wlan['channel']}" if wlan.get("channel") else ""
        sig_txt = f" · {rssi_dbm} dBm" if rssi_dbm is not None else (f" · {signal_pct}%" if signal_pct is not None else "")
        w2_summary = f"{band_txt}{ch_txt}{sig_txt}"

        if band_ghz is not None and band_ghz < 4.0:
            w2_faults.append(
                FaultItem(
                    code="WIFI_BAND_24GHZ",
                    layer="W2",
                    severity=Severity.WARN,
                    title="当前处于 2.4 GHz 频段",
                    message="2.4 GHz 易受蓝牙/同频干扰导致画面卡顿，建议切换 5 GHz 或开启 5GHz 热点",
                )
            )
        if (signal_pct is not None and int(signal_pct) < 60) or (rssi_dbm is not None and int(rssi_dbm) < -70):
            w2_faults.append(
                FaultItem(
                    code="WIFI_SIGNAL_WEAK",
                    layer="W2",
                    severity=Severity.WARN,
                    title="无线信号偏弱",
                    message="当前信号强度偏弱，建议靠近路由器或改用 5 GHz 电脑热点贴脸直连",
                    details={"signal_pct": signal_pct, "rssi_dbm": rssi_dbm},
                )
            )
        min_rate = min(
            [r for r in (rx_mbps, tx_mbps) if isinstance(r, (int, float)) and r > 0],
            default=None,
        )
        if min_rate is not None and min_rate < 150:
            w2_faults.append(
                FaultItem(
                    code="WIFI_LINK_SLOW",
                    layer="W2",
                    severity=Severity.WARN,
                    title="无线协商速率偏低",
                    message=f"当前协商速率 {int(min_rate)} Mbps，高码率串流建议先切换 5 GHz / Wi-Fi 6",
                )
            )

    w2 = LayerReport(
        layer="W2",
        name="频段与信号",
        status=_layer_status_from_faults(w2_faults, w2_base),
        summary=w2_summary,
        faults=w2_faults,
        metrics=w2_metrics,
    )

    # ---------------- W3: Hotspot State OR Same-LAN Reachability / AP Isolation ----------------
    w3_faults: list[FaultItem] = []
    hs_state = str(hotspot.get("state", "Off"))
    hs_on = hs_state.lower() == "on"
    hs_band = str(hotspot.get("band", "Auto"))
    hs_5g_ok = bool(hotspot.get("band_5ghz_supported", False))
    hs_ssid = str(hotspot.get("ssid", ""))
    hs_clients_count = int(hotspot.get("client_count") or len(hotspot_clients))

    w3_metrics: dict[str, Any] = {
        "submode": submode,
        "hotspot_state": hs_state,
        "hotspot_band": hs_band,
        "hotspot_5ghz_supported": hs_5g_ok,
        "hotspot_ssid": hs_ssid,
        "hotspot_client_count": hs_clients_count,
        "hotspot_clients": hotspot_clients,
        "wired_nat_active": wired_nat_active,
        "virtual_adapter_ip": hotspot.get("virtual_adapter_ip", ""),
    }

    if submode == "hotspot":
        if wired_nat_active:
            w3_base = Severity.FAIL
            w3_summary = "有线 WinNAT 冲突"
            w3_faults.append(
                FaultItem(
                    code="WIRED_NAT_CONFLICT",
                    layer="W3",
                    severity=Severity.FAIL,
                    title="有线直连共享运行中",
                    message="有线 WinNAT 与移动热点不能同时开启，请先点关闭有线直连",
                    fix_action="disable-direct",
                )
            )
        elif hotspot.get("capability", "Enabled") not in ("Enabled", "Unknown") and not hs_on:
            w3_base = Severity.FAIL
            w3_summary = "热点不可用"
            w3_faults.append(
                FaultItem(
                    code="HOTSPOT_UNSUPPORTED",
                    layer="W3",
                    severity=Severity.FAIL,
                    title="当前无法开启移动热点",
                    message="请先确认电脑已连接上游网络且策略允许开启移动热点",
                    details={"capability": hotspot.get("capability")},
                )
            )
        elif hs_on:
            w3_base = Severity.OK
            w3_summary = f"{hs_band} · {hs_clients_count} 台已连"
            if "two" in hs_band.lower() or "2.4" in hs_band:
                if hs_5g_ok:
                    w3_faults.append(
                        FaultItem(
                            code="HOTSPOT_BAND_24GHZ",
                            layer="W3",
                            severity=Severity.WARN,
                            title="热点正运行在 2.4 GHz",
                            message="本机支持 5 GHz 热点，建议重建为 5 GHz 以获得高带宽低延迟",
                            fix_action="enable-hotspot",
                        )
                    )
        else:
            w3_base = Severity.IDLE
            w3_summary = "热点未开启"
    else:
        # LAN submode: inspect gateway vs peer to catch AP isolation!
        target_peer = str(connectivity.get("target_peer_ip") or cfg.wireless_peer_ip or "")
        gw_ok = connectivity.get("gateway_reachable")
        peer_ok = connectivity.get("peer_reachable")
        w3_metrics["lan_peer_ip"] = target_peer
        if w2.status == Severity.FAIL:
            w3_base = Severity.IDLE
            w3_summary = "待 Wi-Fi 连接"
        elif target_peer and gw_ok is True and peer_ok is False and wifi_ip and _same_subnet(wifi_ip, wifi_prefix, target_peer):
            w3_base = Severity.WARN
            w3_summary = "疑似 AP 隔离"
            w3_faults.append(
                FaultItem(
                    code="LAN_AP_ISOLATION_SUSPECTED",
                    layer="W3",
                    severity=Severity.WARN,
                    title="疑似路由器/校园网 AP 隔离",
                    message="网关正常但同网段设备不可达，若在校园网建议改用上方「5GHz 电脑热点」",
                    details={"wifi_ip": wifi_ip, "peer_ip": target_peer},
                )
            )
        else:
            w3_base = Severity.OK
            w3_summary = f"同网段 ({wifi_ip or 'Wi-Fi'})"

    w3 = LayerReport(
        layer="W3",
        name="热点/局域网",
        status=_layer_status_from_faults(w3_faults, w3_base),
        summary=w3_summary,
        faults=w3_faults,
        metrics=w3_metrics,
    )

    # ---------------- W4: Interference Gate & Anti-Stutter Status ----------------
    w4_faults: list[FaultItem] = []
    tun_routes = [
        r
        for r in default_routes
        if r.get("is_virtual_or_tun")
        or TUN_KEYWORD_RE.search(f"{r.get('interface_alias', '')} {r.get('interface_description', '')}")
    ]
    jitter_ms = connectivity.get("jitter_ms")
    loss_pct = connectivity.get("loss_pct")
    w4_metrics: dict[str, Any] = {
        "autoconfig_enabled": autoconfig_enabled,
        "tun_routes": tun_routes,
        "jitter_ms": jitter_ms,
        "loss_pct": loss_pct,
        "anti_stutter_active": (autoconfig_enabled is False) and not (wifi and wifi.get("powersave_enabled")),
    }

    if tun_routes:
        tun_alias = str(tun_routes[0].get("interface_alias", "VPN TUN"))
        w4_faults.append(
            FaultItem(
                code="VPN_TUN_ACTIVE",
                layer="W4",
                severity=Severity.FAIL if submode == "hotspot" else Severity.WARN,
                title="VPN 正在接管路由",
                message="开启热点直连或本地串流建议先断开 VPN，以免流量兜圈",
                details={"tun_adapter": tun_alias},
            )
        )
    if connected and autoconfig_enabled is True:
        w4_faults.append(
            FaultItem(
                code="WIFI_BG_SCAN_ACTIVE",
                layer="W4",
                severity=Severity.WARN,
                title="系统周期后台扫描开启中",
                message="Windows 每隔几十秒扫描周边 Wi-Fi 易引发周期性卡顿，点「无线防抖」一键暂停",
                fix_action="optimize-wifi",
            )
        )
    if (isinstance(jitter_ms, (int, float)) and jitter_ms > 15.0) or (
        isinstance(loss_pct, (int, float)) and loss_pct > 2.0
    ):
        w4_faults.append(
            FaultItem(
                code="WIFI_JITTER_HIGH",
                layer="W4",
                severity=Severity.WARN,
                title="无线链路抖动偏高",
                message=f"实测抖动 {jitter_ms} ms / 丢包 {loss_pct}%，建议切 5 GHz 热点或换用干净信道",
                details={"jitter_ms": jitter_ms, "loss_pct": loss_pct},
            )
        )

    w4_base = Severity.OK
    if w4_faults:
        w4_summary = "需处理" if any(f.severity == Severity.FAIL for f in w4_faults) else "可优化防抖"
    else:
        w4_summary = "防抖已生效 · 无冲突" if w4_metrics["anti_stutter_active"] else "无冲突"

    w4 = LayerReport(
        layer="W4",
        name="防抖与门禁",
        status=_layer_status_from_faults(w4_faults, w4_base),
        summary=w4_summary,
        faults=w4_faults,
        metrics=w4_metrics,
    )

    # ---------------- W5: Peer Device & Service Endpoints ----------------
    w5_faults: list[FaultItem] = []
    target_peer = str(connectivity.get("target_peer_ip") or "")
    peer_ok = connectivity.get("peer_reachable")
    inet_ok = connectivity.get("host_internet_ok")
    svc_ok = connectivity.get("service_endpoint_ok")
    svc_status = connectivity.get("service_endpoint_status")
    w5_metrics: dict[str, Any] = {
        "preset": preset.id,
        "target_peer_ip": target_peer,
        "peer_reachable": peer_ok,
        "peer_rtt_ms": connectivity.get("peer_rtt_ms"),
        "jitter_ms": jitter_ms,
        "host_internet_ok": inet_ok,
        "service_endpoint_ok": svc_ok,
    }

    if inet_ok is False:
        w5_faults.append(
            FaultItem(
                code="HOST_INTERNET_DOWN",
                layer="W5",
                severity=Severity.WARN,
                title="笔记本暂未联网",
                message="请确认上游 Wi-Fi / 热点能正常访问互联网",
            )
        )
    if target_peer:
        if peer_ok is False:
            w5_faults.append(
                FaultItem(
                    code="WIFI_PEER_UNREACHABLE",
                    layer="W5",
                    severity=Severity.WARN,
                    title="无线设备暂未响应",
                    message=f"无法 ping 通 {target_peer}，请确认设备已连接当前无线并开机",
                    details={"peer_ip": target_peer},
                )
            )
        elif peer_ok is True and svc_ok is False and preset.service_fault_code:
            svc_copy = {
                "XBOX_LIVE_UNREACHABLE": ("Xbox Live 不可达", "请检查上游网络能否访问 Xbox 服务"),
                "PSN_UNREACHABLE": ("PSN 认证暂不可达", "请检查上游网络能否访问 PlayStation 服务"),
                "NINTENDO_UNREACHABLE": ("任天堂连通检测失败", "请检查上游 DNS 或网络连通性"),
            }
            ft, fm = svc_copy.get(preset.service_fault_code, ("平台服务不可达", "请检查上游网络"))
            w5_faults.append(
                FaultItem(
                    code=preset.service_fault_code,
                    layer="W5",
                    severity=Severity.WARN,
                    title=ft,
                    message=fm,
                    details={"status": svc_status, "endpoint": preset.service_endpoint_url},
                )
            )

    if peer_ok is True:
        rtt = connectivity.get("peer_rtt_ms")
        w5_base = Severity.OK
        w5_summary = f"{target_peer} · {rtt:.1f} ms" if isinstance(rtt, (int, float)) else target_peer
    elif not target_peer:
        w5_base = Severity.IDLE
        w5_summary = "等待设备接入" if submode == "hotspot" else "未填设备 IP"
    elif peer_ok is None:
        w5_base = Severity.IDLE
        w5_summary = "未探测"
    else:
        w5_base = Severity.IDLE
        w5_summary = f"{target_peer} 未响应"

    active_w5_faults = [f for f in w5_faults if f.severity in (Severity.WARN, Severity.FAIL)]
    w5 = LayerReport(
        layer="W5",
        name="设备与出口",
        status=_layer_status_from_faults(active_w5_faults, w5_base),
        summary=w5_summary,
        faults=w5_faults,
        metrics=w5_metrics,
    )

    # ---------------- W6: Preset-Specific Readiness ----------------
    w6_faults: list[FaultItem] = []
    custom_path = cfg.custom_client_path.strip()
    custom_ok = bool(custom_path and Path(custom_path).is_file())
    w6_metrics: dict[str, Any] = {
        "preset": preset.id,
        "custom_client_path": custom_path if custom_ok else None,
        "xstreaming_path": stream_clients.get("xstreaming_path"),
        "xbox_app_installed": bool(stream_clients.get("xbox_app_installed", False)),
        "chiaki_path": stream_clients.get("chiaki_path"),
        "ps_remote_path": stream_clients.get("ps_remote_path"),
        "moonlight_path": stream_clients.get("moonlight_path"),
        "sunshine_path": stream_clients.get("sunshine_path"),
        "steam_path": stream_clients.get("steam_path"),
        "uwp_missing_count": len(uwp_missing) if preset.uses_uwp_loopback else 0,
    }
    if not preset.uses_uwp_loopback and not preset.uses_stream_client:
        w6_base = Severity.SKIPPED
        w6_summary = "无需串流端" if preset.id == "switch" else "通用模式"
        w6_metrics["skipped_reason"] = f"preset={preset.id}"
    else:
        w6_base = Severity.OK
        if preset.uses_uwp_loopback and uwp_missing:
            w6_faults.append(
                FaultItem(
                    code="UWP_LOOPBACK_MISSING",
                    layer="W6",
                    severity=Severity.WARN,
                    title="商店/Xbox 未解除隔离",
                    message="开代理时商店与 Xbox 会打不开，点一键豁免",
                    fix_action="fix-uwp",
                    details={"missing_packages": uwp_missing},
                )
            )
        if preset.uses_stream_client and preset.client_fault_code:
            has_client = custom_ok or any(bool(stream_clients.get(k)) for k in preset.client_keys)
            if not has_client:
                client_copy = {
                    "STREAM_CLIENT_MISSING": ("未装串流客户端", "建议下载官方 XStreaming 或安装 Xbox 应用"),
                    "PS_CLIENT_MISSING": ("未装 PS 串流端", "建议安装 Chiaki-ng 或官方 PS Remote Play"),
                    "HANDHELD_CLIENT_MISSING": ("未装串流服务端", "建议安装 Moonlight / Sunshine 或 Steam"),
                }
                ct, cm = client_copy.get(preset.client_fault_code, ("未装客户端", "请在首次向导中准备客户端"))
                w6_faults.append(
                    FaultItem(
                        code=preset.client_fault_code,
                        layer="W6",
                        severity=Severity.WARN,
                        title=ct,
                        message=cm,
                        fix_action="download-stream",
                    )
                )
        w6_summary = "已就绪" if not w6_faults else "待准备"

    w6 = LayerReport(
        layer="W6",
        name="串流",
        status=_layer_status_from_faults(w6_faults, w6_base),
        summary=w6_summary,
        faults=w6_faults,
        metrics=w6_metrics,
    )

    layers = [w1, w2, w3, w4, w5, w6]
    base_statuses = {
        "W1": w1_base,
        "W2": w2_base,
        "W3": w3_base,
        "W4": w4_base,
        "W5": w5_base,
        "W6": w6_base,
    }

    # Merge any matching AI extensions (supports W1..W6 and L5/L6 mapped to W5/W6)
    serialized_exts: list[dict[str, Any]] = []
    if ext_results:
        layer_map = {layer.layer: layer for layer in layers}
        alias_map = {"L5": w5, "L6": w6}
        for er in ext_results:
            target = layer_map.get(er.layer) or alias_map.get(er.layer)
            if target is None:
                continue
            serialized_exts.append(er.to_dict())
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
                        details=er.details,
                    )
                )
                target.status = _layer_status_from_faults(target.faults, base_statuses[target.layer])

    # Determine wireless mode & primary fault
    can_operate = w1.status != Severity.FAIL and not wired_nat_active
    if w1.status == Severity.FAIL or w3.status == Severity.FAIL or (submode == "lan" and w2.status == Severity.FAIL) or w4.status == Severity.FAIL:
        mode = "blocked"
    elif hs_on:
        mode = "hotspot"
    elif w4_metrics["anti_stutter_active"]:
        mode = "optimized"
    else:
        mode = "normal"

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
        headline = f"检测到无线断点：{primary_fault.title}"
    elif mode == "hotspot":
        headline = f"5 GHz 热点直连运行中 — {hs_clients_count} 台设备已连"
    elif mode == "optimized":
        headline = "无线防抖优化生效中 — 随时开玩"
    else:
        headline = "无线网卡就绪 — 可开启 5GHz 热点或防抖优化"

    return DiagnosticReport(
        schema_version=SCHEMA_VERSION,
        app_version=APP_VERSION,
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
        transport="wireless",
        console_profile=cfg.console_profile,
        mode=mode,
        can_enable_direct=can_operate,
        headline=headline,
        primary_fault=primary_fault,
        layers=layers,
        snapshot=snapshot,
        extensions=serialized_exts,
    )


def run_wireless_diagnosis(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    include_extensions: bool = True,
    probe_network: bool = True,
    snapshot_fn: Callable[..., dict[str, Any]] = collect_wireless_snapshot,
) -> DiagnosticReport:
    """Collect wireless snapshot, run auto_check extensions, and classify wireless layers."""
    snapshot = snapshot_fn(cfg, probe_network=probe_network)
    ext_results: list[ExtensionRunResult] = []
    if include_extensions:
        try:
            from wiredeck.extensions import run_auto_check_extensions

            ext_results = run_auto_check_extensions(paths)
        except ImportError:
            ext_results = []
    return classify_wireless_snapshot(snapshot, cfg, ext_results=ext_results)
