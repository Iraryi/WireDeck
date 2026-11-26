from __future__ import annotations

from typing import Any

import pytest

from wiredeck.config import AppConfig
from wiredeck.diagnostics import classify_snapshot
from wiredeck.models import ExtensionRunResult, Severity
from wiredeck.version import APP_VERSION, NAT_NAME, SCHEMA_VERSION


def _healthy_snapshot(direct_active: bool = False) -> dict[str, Any]:
    return {
        "os_version": "Windows 11 23H2",
        "is_admin": False,
        "net_adapters": [
            {
                "name": "以太网",
                "interface_description": "Realtek Gaming 2.5GbE Family Controller",
                "interface_index": 12,
                "status": "Up",
                "link_speed_bps": 1_000_000_000,
                "link_speed_text": "1 Gbps",
                "pnp_device_id": "PCI\\VEN_10EC&DEV_8125&SUBSYS_0001\\4&1234",
                "is_virtual": False,
                "is_wireless": False,
                "rx_errors": 0,
                "tx_errors": 0,
                "rx_discards": 0,
                "tx_discards": 0,
                "powersave_enabled": [],
            },
            {
                "name": "WLAN",
                "interface_description": "Intel(R) Wi-Fi 6E AX211 160MHz",
                "interface_index": 8,
                "status": "Up",
                "link_speed_bps": 866_000_000,
                "link_speed_text": "866 Mbps",
                "pnp_device_id": "PCI\\VEN_8086&DEV_51F1\\3&11583659",
                "is_virtual": False,
                "is_wireless": True,
                "rx_errors": 0,
                "tx_errors": 0,
                "rx_discards": 0,
                "tx_discards": 0,
                "powersave_enabled": [],
            },
        ],
        "pnp_ethernet_devices": [
            {
                "friendly_name": "Realtek Gaming 2.5GbE Family Controller",
                "instance_id": "PCI\\VEN_10EC&DEV_8125&SUBSYS_0001\\4&1234",
                "status": "OK",
                "problem_code": 0,
                "parent_instance_id": "PCI\\VEN_8086&DEV_7AB0\\3&11583659",
                "parent_friendly_name": "Intel(R) PCI Express Root Port #15",
                "parent_problem_code": 0,
                "parent_status": "OK",
            }
        ],
        "ipv4_addresses": [
            {"interface_alias": "以太网", "interface_index": 12, "ip": "192.168.50.2", "prefix_length": 24},
            {"interface_alias": "WLAN", "interface_index": 8, "ip": "192.168.8.104", "prefix_length": 24},
        ],
        "default_routes": [
            {
                "interface_alias": "WLAN",
                "interface_index": 8,
                "next_hop": "192.168.8.1",
                "metric": 35,
                "is_virtual_or_tun": False,
                "interface_description": "Intel(R) Wi-Fi 6E AX211 160MHz",
            }
        ],
        "ip_forwarding": {
            "以太网": direct_active,
            "WLAN": direct_active,
        },
        "ics_sharing_interfaces": [],
        "shared_access_service_status": "Running",  # Idle Win11 Hyper-V false-positive guard
        "udp_listeners": [],
        "net_nats": (
            [{"name": NAT_NAME, "internal_subnet": "192.168.50.0/24"}] if direct_active else []
        ),
        "firewall_rules": {
            "WireDeck-WinNAT-Allow": direct_active,
            "WireDeck-Direct-Allow": direct_active,
        },
        "uwp_exempt_packages": [
            "microsoft.windowsstore_8wekyb3d8bbwe",
            "microsoft.gamingapp_8wekyb3d8bbwe",
            "microsoft.xboxidentityprovider_8wekyb3d8bbwe",
            "microsoft.aad.brokerplugin_cw5n1h2txyewy",
            "microsoft.accountscontrol_cw5n1h2txyewy",
            "microsoft.gamingservices_8wekyb3d8bbwe",
            "microsoft.xboxgamingoverlay_8wekyb3d8bbwe",
            "microsoft.xboxspeechtotextoverlay_8wekyb3d8bbwe",
            "microsoft.xbox.tcui_8wekyb3d8bbwe",
        ],
        "uwp_missing_packages": [],
        "stream_clients": {
            "xstreaming_path": "C:\\Program Files\\XStreaming\\XStreaming.exe",
            "xbox_app_installed": True,
        },
        "connectivity": {
            "peer_reachable": True,
            "peer_rtt_ms": 0.42,
            "host_internet_ok": True,
            "xbox_live_ok": True,
            "xbox_live_status": 405,
        },
    }


def test_healthy_direct_mode_is_all_green():
    cfg = AppConfig()
    report = classify_snapshot(_healthy_snapshot(direct_active=True), cfg)
    assert report.schema_version == SCHEMA_VERSION
    assert report.app_version == APP_VERSION
    assert report.mode == "direct"
    assert report.can_enable_direct is True
    assert report.primary_fault is None
    assert len(report.layers) == 6
    assert all(layer.status == Severity.OK for layer in report.layers)


def test_idle_hyperv_shared_access_does_not_trigger_ics_false_positive():
    cfg = AppConfig()
    snap = _healthy_snapshot(direct_active=False)
    snap["shared_access_service_status"] = "Running"
    snap["ics_sharing_interfaces"] = []
    snap["udp_listeners"] = []
    report = classify_snapshot(snap, cfg)
    l3 = next(l for l in report.layers if l.layer == "L3")
    l4 = next(l for l in report.layers if l.layer == "L4")
    assert l3.status == Severity.OK
    assert l4.status == Severity.IDLE
    assert report.mode == "normal"
    assert report.can_enable_direct is True


def test_detects_pcie_parent_disabled_code_45_and_22():
    cfg = AppConfig()
    snap = _healthy_snapshot()
    snap["net_adapters"] = [snap["net_adapters"][1]]  # Only WLAN visible
    snap["pnp_ethernet_devices"] = [
        {
            "friendly_name": "Realtek Gaming 2.5GbE Family Controller",
            "instance_id": "PCI\\VEN_10EC&DEV_8125\\4&1234",
            "status": "Unknown",
            "problem_code": 45,
            "parent_instance_id": "PCI\\VEN_8086&DEV_7AB0\\3&11583659",
            "parent_friendly_name": "Intel(R) PCI Express Root Port #15",
            "parent_problem_code": 22,
            "parent_status": "Error",
        }
    ]
    report = classify_snapshot(snap, cfg)
    l1 = next(l for l in report.layers if l.layer == "L1")
    assert l1.status == Severity.FAIL
    assert any(f.code == "NIC_PCIE_PARENT_DISABLED" and f.fix_action == "fix-nic" for f in l1.faults)
    assert report.primary_fault is not None
    assert report.primary_fault.code == "NIC_PCIE_PARENT_DISABLED"


def test_multi_physical_nics_picks_active_usb_nic_over_disabled_onboard():
    cfg = AppConfig()
    snap = _healthy_snapshot(direct_active=True)
    snap["net_adapters"] = [
        {
            "name": "以太网",
            "interface_description": "Onboard Disabled NIC",
            "interface_index": 12,
            "status": "Disabled",
            "link_speed_bps": 0,
            "link_speed_text": "0 bps",
            "pnp_device_id": "PCI\\VEN_10EC&DEV_8125\\4&1234",
            "is_virtual": False,
            "is_wireless": False,
            "rx_errors": 0,
            "tx_errors": 0,
            "rx_discards": 0,
            "tx_discards": 0,
            "powersave_enabled": [],
        },
        {
            "name": "以太网 2",
            "interface_description": "ASIX USB-C Gigabit Ethernet",
            "interface_index": 20,
            "status": "Up",
            "link_speed_bps": 1_000_000_000,
            "link_speed_text": "1 Gbps",
            "pnp_device_id": "USB\\VID_0B95&PID_1790\\001",
            "is_virtual": False,
            "is_wireless": False,
            "rx_errors": 0,
            "tx_errors": 0,
            "rx_discards": 0,
            "tx_discards": 0,
            "powersave_enabled": [],
        },
        snap["net_adapters"][1],
    ]
    snap["ipv4_addresses"][0]["interface_alias"] = "以太网 2"
    snap["ipv4_addresses"][0]["interface_index"] = 20
    snap["ip_forwarding"] = {"以太网 2": True, "WLAN": True}

    report = classify_snapshot(snap, cfg)
    l1 = next(l for l in report.layers if l.layer == "L1")
    l2 = next(l for l in report.layers if l.layer == "L2")
    assert l1.status == Severity.OK
    assert l1.metrics["selected_ethernet"] == "以太网 2"
    assert l2.status == Severity.OK


def test_detects_100m_degraded_link_and_powersave_warning():
    cfg = AppConfig()
    snap = _healthy_snapshot()
    snap["net_adapters"][0]["link_speed_bps"] = 100_000_000
    snap["net_adapters"][0]["link_speed_text"] = "100 Mbps"
    snap["net_adapters"][0]["powersave_enabled"] = ["*EEE", "GigaLite"]
    report = classify_snapshot(snap, cfg)
    l1 = next(l for l in report.layers if l.layer == "L1")
    l2 = next(l for l in report.layers if l.layer == "L2")
    assert l1.status == Severity.WARN
    assert any(f.code == "NIC_POWERSAVE_ENABLED" for f in l1.faults)
    assert l2.status == Severity.WARN
    assert any(f.code == "LINK_DEGRADED_100M" for f in l2.faults)


@pytest.mark.parametrize(
    "desc",
    ["LightXtreme sing-tun", "Wintun Userspace Tunnel", "Clash Meta TUN", "WireGuard Tunnel"],
)
def test_detects_any_vpn_tun_holding_default_route(desc: str):
    cfg = AppConfig()
    snap = _healthy_snapshot()
    snap["default_routes"].append(
        {
            "interface_alias": "Tunnel0",
            "interface_index": 33,
            "next_hop": "172.19.0.2",
            "metric": 5,
            "is_virtual_or_tun": True,
            "interface_description": desc,
        }
    )
    report = classify_snapshot(snap, cfg)
    l3 = next(l for l in report.layers if l.layer == "L3")
    assert l3.status == Severity.FAIL
    assert any(f.code == "VPN_TUN_ACTIVE" for f in l3.faults)
    assert report.can_enable_direct is False
    assert report.mode == "blocked"


def test_port_53_ics_blocks_but_third_party_dns_downgrades_to_warn():
    cfg = AppConfig()

    # Case 1: SharedAccess owns UDP 53 -> hard block
    snap_ics = _healthy_snapshot()
    snap_ics["udp_listeners"] = [
        {"port": 53, "pid": 4120, "process_name": "svchost", "service_name": "SharedAccess"}
    ]
    rep_ics = classify_snapshot(snap_ics, cfg)
    l3_ics = next(l for l in rep_ics.layers if l.layer == "L3")
    assert l3_ics.status == Severity.FAIL
    assert any(f.code == "PORT_53_67_OCCUPIED" for f in l3_ics.faults)
    assert rep_ics.can_enable_direct is False

    # Case 2: Non-ICS third-party SmartDNS on UDP 53 with ICS off -> warn, NOT blocking
    snap_3p = _healthy_snapshot()
    snap_3p["udp_listeners"] = [
        {"port": 53, "pid": 7788, "process_name": "AdGuardHome", "service_name": "AdGuardHome"}
    ]
    rep_3p = classify_snapshot(snap_3p, cfg)
    l3_3p = next(l for l in rep_3p.layers if l.layer == "L3")
    assert l3_3p.status == Severity.WARN
    assert any(f.code == "PORT_53_NON_ICS_WARNING" for f in l3_3p.faults)
    assert rep_3p.can_enable_direct is True

    # Case 3: Any listener on UDP 67 -> hard block
    snap_67 = _healthy_snapshot()
    snap_67["udp_listeners"] = [
        {"port": 67, "pid": 9999, "process_name": "dhcpd", "service_name": "CustomDHCP"}
    ]
    rep_67 = classify_snapshot(snap_67, cfg)
    assert rep_67.can_enable_direct is False
    assert any(f.code == "PORT_53_67_OCCUPIED" for f in rep_67.layers[2].faults)


def test_foreign_netnat_blocks_with_owner_name():
    cfg = AppConfig()
    snap = _healthy_snapshot()
    snap["net_nats"] = [{"name": "WSLNat", "internal_subnet": "172.24.0.0/20"}]
    report = classify_snapshot(snap, cfg)
    l3 = next(l for l in report.layers if l.layer == "L3")
    assert l3.status == Severity.FAIL
    fault = next(f for f in l3.faults if f.code == "FOREIGN_NETNAT_EXISTS")
    assert "WSLNat" in fault.message or fault.details.get("nat_names") == ["WSLNat"]
    assert fault.fix_action == "clear-netnat"
    assert report.can_enable_direct is False


@pytest.mark.parametrize("profile", ["ps5", "generic"])
def test_non_xbox_profiles_mark_xbox_only_checks_skipped(profile: str):
    cfg = AppConfig(console_profile=profile)
    snap = _healthy_snapshot(direct_active=True)
    snap["uwp_missing_packages"] = ["microsoft.gamingapp_8wekyb3d8bbwe"]
    snap["stream_clients"] = {"xstreaming_path": None, "xbox_app_installed": False}
    snap["connectivity"]["xbox_live_ok"] = False

    report = classify_snapshot(snap, cfg)
    l5 = next(l for l in report.layers if l.layer == "L5")
    l6 = next(l for l in report.layers if l.layer == "L6")
    assert l5.status == Severity.OK
    assert l6.status == Severity.SKIPPED
    assert report.primary_fault is None


def test_extension_merges_and_overrides_builtin_fault():
    cfg = AppConfig()
    snap = _healthy_snapshot(direct_active=True)
    snap["net_adapters"][0]["powersave_enabled"] = ["*EEE"]

    ext_res = ExtensionRunResult(
        ext_id="fix-intel-i225",
        name="修复 Intel I225-V 节能断流",
        author="DSH",
        layer="L1",
        status=Severity.FAIL,
        code="EXT_INTEL_I225_EEE",
        title="I225 节能异常",
        message="检测到 I225-V 节能注册表项未关闭",
        fix_available=True,
        requires_admin=True,
        overrides_faults=["NIC_POWERSAVE_ENABLED"],
    )
    report = classify_snapshot(snap, cfg, ext_results=[ext_res])
    l1 = next(l for l in report.layers if l.layer == "L1")
    assert l1.status == Severity.FAIL
    codes = [f.code for f in l1.faults]
    assert "EXT_INTEL_I225_EEE" in codes
    assert "NIC_POWERSAVE_ENABLED" not in codes
    ext_fault = next(f for f in l1.faults if f.code == "EXT_INTEL_I225_EEE")
    assert ext_fault.source == "extension"
    assert ext_fault.ext_id == "fix-intel-i225"
    assert ext_fault.fix_action == "run-ext"
    assert ext_fault.overridden_from == ["NIC_POWERSAVE_ENABLED"]


def test_parse_uwp_exemptions_works_on_chinese_and_english_windows():
    from wiredeck.probes import parse_uwp_exemptions

    sample = (
        "[1] -----\n"
        "    名称: microsoft.windowsstore_8wekyb3d8bbwe\n"
        "    SID:  S-1-15-2-123\n"
        "[2] -----\n"
        "    Name: Microsoft.GamingApp_8wekyb3d8bbwe\n"
        "[3] -----\n"
        "    名称: AppContainer NOT FOUND\n"
    )
    parsed = parse_uwp_exemptions(sample)
    assert parsed == [
        "microsoft.gamingapp_8wekyb3d8bbwe",
        "microsoft.windowsstore_8wekyb3d8bbwe",
    ]


def test_select_ethernet_avoids_usb_rndis_hotspot_holding_default_route():
    cfg = AppConfig()
    snap = _healthy_snapshot(direct_active=False)
    # Onboard RJ45 is Disconnected (console off), while USB phone hotspot is Up & holds 0.0.0.0/0
    snap["net_adapters"] = [
        {
            "name": "以太网",
            "interface_description": "Realtek Gaming 2.5GbE Family Controller",
            "interface_index": 12,
            "status": "Disconnected",
            "link_speed_bps": 0,
            "link_speed_text": "0 bps",
            "pnp_device_id": "PCI\\VEN_10EC&DEV_8125",
            "is_virtual": False,
            "is_wireless": False,
            "rx_errors": 0,
            "tx_errors": 0,
            "rx_discards": 0,
            "tx_discards": 0,
            "powersave_enabled": [],
        },
        {
            "name": "以太网 3",
            "interface_description": "Remote NDIS based Internet Sharing Device",
            "interface_index": 25,
            "status": "Up",
            "link_speed_bps": 425_000_000,
            "link_speed_text": "425 Mbps",
            "pnp_device_id": "USB\\VID_18D1",
            "is_virtual": False,
            "is_wireless": False,
            "rx_errors": 0,
            "tx_errors": 0,
            "rx_discards": 0,
            "tx_discards": 0,
            "powersave_enabled": [],
        },
    ]
    snap["default_routes"] = [
        {
            "interface_alias": "以太网 3",
            "interface_index": 25,
            "next_hop": "192.168.42.129",
            "metric": 25,
            "is_virtual_or_tun": False,
            "interface_description": "Remote NDIS based Internet Sharing Device",
        }
    ]
    report = classify_snapshot(snap, cfg)
    l1 = next(l for l in report.layers if l.layer == "L1")
    assert l1.metrics["selected_ethernet"] == "以太网"
    assert l1.metrics["selected_uplink"] == "以太网 3"


def test_summary_refreshes_on_extension_override_and_host_internet_down():
    cfg = AppConfig()
    snap = _healthy_snapshot(direct_active=True)
    snap["net_adapters"][0]["powersave_enabled"] = ["*EEE"]
    snap["connectivity"]["host_internet_ok"] = False

    ext_ok = ExtensionRunResult(
        ext_id="fix-eee",
        name="自定义节能检测",
        author="DSH",
        layer="L1",
        status=Severity.OK,
        code="OK",
        title="正常",
        message="已确认无需处理",
        overrides_faults=["NIC_POWERSAVE_ENABLED"],
    )
    rep = classify_snapshot(snap, cfg, ext_results=[ext_ok])
    l1 = next(l for l in rep.layers if l.layer == "L1")
    l5 = next(l for l in rep.layers if l.layer == "L5")
    assert l1.status == Severity.OK
    assert l1.summary == "正常"
    assert l5.status == Severity.FAIL
    assert l5.summary == "外网不通"


def test_ping_parser_handles_cp936_chinese_and_rejects_destination_unreachable():
    from wiredeck.probes import parse_ping_output

    ok_cn, rtt_cn = parse_ping_output("来自 192.168.50.1 的回复: 字节=32 时间<1ms TTL=128")
    assert ok_cn is True and rtt_cn == 0.5

    ok_en, rtt_en = parse_ping_output("Reply from 192.168.50.1: bytes=32 time=0.4ms TTL=128")
    assert ok_en is True and rtt_en == 0.4

    # Windows ping exits 0 on 'Destination host unreachable' (no TTL= / time=)
    ok_unreach, rtt_unreach = parse_ping_output("来自 192.168.50.2 的回复: 无法访问目标主机。")
    assert ok_unreach is False and rtt_unreach is None


