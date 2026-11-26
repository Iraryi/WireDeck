from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wiredeck.actions import SUPPORTED_ACTIONS, execute_action
from wiredeck.config import AppConfig, load_config, save_config
from wiredeck.exporter import export_diagnostic_bundle
from wiredeck.models import Severity
from wiredeck.paths import resolve_app_paths
from wiredeck.ps_runner import PsResult
from wiredeck.toolkits import discover_all_toolkits, run_toolkit
from wiredeck.wireless_actions import disable_hotspot, enable_hotspot, optimize_wifi, restore_wifi
from wiredeck.wireless_diagnostics import classify_wireless_snapshot
from wiredeck.wireless_probes import (
    detect_suboptimal_wifi_props,
    parse_netsh_autoconfig,
    parse_netsh_wlan_interfaces,
    parse_ping_series,
)


def _ok_ps(stdout: str = "OK\n") -> PsResult:
    return PsResult(returncode=0, stdout=stdout, stderr="", timed_out=False)


def _base_wireless_snap(**overrides: Any) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "is_admin": True,
        "wifi_adapters": [
            {
                "name": "WLAN",
                "interface_description": "Intel(R) Wi-Fi 6 AX201 160MHz",
                "interface_index": 11,
                "status": "Up",
                "link_speed_bps": 866_000_000,
                "link_speed_text": "866 Mbps",
                "pnp_device_id": "PCI\\VEN_8086&DEV_A0F0",
                "advanced_properties": [
                    {"keyword": "MIMOPowerSaveMode", "display_value": "无 SMPS", "registry_value": "0"},
                    {"keyword": "*PacketCoalescing", "display_value": "已禁用", "registry_value": "0"},
                ],
                "powersave_enabled": [],
            }
        ],
        "wlan": {
            "name": "WLAN",
            "description": "Intel(R) Wi-Fi 6 AX201 160MHz",
            "state": "connected",
            "connected": True,
            "ssid": "Studio-5G",
            "band": "5 GHz",
            "band_ghz": 5.0,
            "channel": 157,
            "radio_type": "802.11ax",
            "rx_rate_mbps": 866.0,
            "tx_rate_mbps": 866.0,
            "signal_pct": 85,
            "rssi_dbm": -52,
        },
        "autoconfig_enabled": False,
        "ipv4_addresses": [{"interface_alias": "WLAN", "interface_index": 11, "ip": "192.168.1.105", "prefix_length": 24}],
        "wifi_gateway_ip": "192.168.1.1",
        "default_routes": [],
        "wired_nat_active": False,
        "hotspot": {
            "supported": True,
            "band_5ghz_supported": True,
            "capability": "Enabled",
            "state": "On",
            "band": "FiveGigahertz",
            "ssid": "WireDeck-5G",
            "client_count": 1,
            "max_client_count": 8,
            "virtual_adapter_name": "Microsoft Wi-Fi Direct Virtual Adapter",
            "virtual_adapter_ip": "192.168.137.1",
        },
        "hotspot_clients": [{"ip": "192.168.137.42", "mac": "00-11-22-33-44-55", "state": "Reachable"}],
        "uwp_exempt_packages": ["microsoft.windowsstore_8wekyb3d8bbwe"],
        "uwp_missing_packages": [],
        "stream_clients": {"xstreaming_path": "C:\\XStreaming.exe", "xbox_app_installed": True},
        "connectivity": {
            "target_peer_ip": "192.168.137.42",
            "peer_reachable": True,
            "peer_rtt_ms": 1.7,
            "jitter_ms": 0.8,
            "loss_pct": 0.0,
            "gateway_ip": "192.168.1.1",
            "gateway_reachable": True,
            "host_internet_ok": True,
            "xbox_live_ok": True,
            "service_endpoint_ok": True,
            "service_endpoint_status": 200,
        },
    }
    snap.update(overrides)
    return snap


def test_parse_netsh_wlan_interfaces_and_autoconfig_en_and_zh():
    en_out = """
    Name                   : WLAN
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    State                  : connected
    SSID                   : Lab_5G
    AP BSSID               : aa:bb:cc:dd:ee:ff
    Band                   : 5 GHz
    Channel                : 149
    Radio type             : 802.11ax
    Receive rate (Mbps)    : 1201
    Transmit rate (Mbps)   : 1201
    Signal                 : 88%
    Rssi                   : -49
    """
    parsed = parse_netsh_wlan_interfaces(en_out)
    assert parsed["connected"] is True
    assert parsed["ssid"] == "Lab_5G"
    assert parsed["band_ghz"] == 5.0
    assert parsed["channel"] == 149
    assert parsed["rx_rate_mbps"] == 1201.0
    assert parsed["signal_pct"] == 88
    assert parsed["rssi_dbm"] == -49

    zh_out = """
    名称                   : WLAN
    状态                   : 已连接
    SSID                   : Dorm_24G
    波段                   : 2.4 GHz
    频道                   : 6
    接收速率 (Mbps)        : 72.2
    传输速率 (Mbps)        : 72.2
    信号                   : 45%
    """
    parsed_zh = parse_netsh_wlan_interfaces(zh_out)
    assert parsed_zh["connected"] is True
    assert parsed_zh["ssid"] == "Dorm_24G"
    assert parsed_zh["band_ghz"] == 2.4
    assert parsed_zh["rx_rate_mbps"] == 72.2

    assert parse_netsh_autoconfig('Auto configuration logic is enabled on interface "WLAN"', "WLAN") is True
    assert parse_netsh_autoconfig('接口 "WLAN" 上已禁用自动配置逻辑', "WLAN") is False

    rtts = parse_ping_series("来自 192.168.137.42 的回复: 字节=32 时间=2ms TTL=64\n来自 192.168.137.42 的回复: 字节=32 时间<1ms TTL=64")
    assert rtts == [2.0, 0.5]

    ja_out = """
    名前                   : Wi-Fi
    説明                   : Intel(R) Wi-Fi 6E AX211 160MHz
    状態                   : 接続されました
    SSID                   : Home_5G_JP
    バンド                 : 5 GHz
    チャネル               : 157
    受信速度 (Mbps)        : 1201
    送信速度 (Mbps)        : 1201
    シグナル               : 90%
    """
    parsed_ja = parse_netsh_wlan_interfaces(ja_out)
    assert parsed_ja["connected"] is True
    assert parsed_ja["ssid"] == "Home_5G_JP"
    assert parsed_ja["band_ghz"] == 5.0
    assert parsed_ja["channel"] == 157
    assert parsed_ja["rx_rate_mbps"] == 1201.0
    assert parsed_ja["signal_pct"] == 90
    assert parse_netsh_autoconfig('インターフェイス "Wi-Fi" で自動構成ロジックは有効です', "Wi-Fi") is True
    assert parse_netsh_autoconfig('インターフェイス "Wi-Fi" で自動構成ロジックは無効です', "Wi-Fi") is False
    assert parse_ping_series("192.168.137.42 からの応答: バイト数 =32 時間 =2ms TTL=64\n192.168.137.42 からの応答: バイト数 =32 時間 <1ms TTL=64") == [2.0, 0.5]


def test_wireless_classifier_healthy_and_fault_rules():
    cfg = AppConfig(active_transport="wireless", wireless_submode="hotspot", active_preset="xbox")
    rep_ok = classify_wireless_snapshot(_base_wireless_snap(), cfg)
    assert rep_ok.transport == "wireless"
    assert rep_ok.mode == "hotspot"
    assert rep_ok.primary_fault is None
    assert all(l.status == Severity.OK for l in rep_ok.layers)

    # Wired NAT conflict blocks hotspot startup
    rep_nat = classify_wireless_snapshot(_base_wireless_snap(wired_nat_active=True), cfg)
    assert rep_nat.mode == "blocked"
    assert rep_nat.primary_fault is not None
    assert rep_nat.primary_fault.code == "WIRED_NAT_CONFLICT"

    # Detect 2.4GHz, weak signal, powersave, background scan & high jitter
    subopt = detect_suboptimal_wifi_props([
        {"keyword": "MIMOPowerSaveMode", "display_value": "自动 SMPS", "registry_value": "1"},
        {"keyword": "*PacketCoalescing", "display_value": "已启用", "registry_value": "1"},
    ])
    assert set(subopt) == {"MIMOPowerSaveMode", "*PacketCoalescing"}

    snap_bad = _base_wireless_snap(
        wlan={"connected": True, "band": "2.4 GHz", "band_ghz": 2.4, "channel": 6, "signal_pct": 42, "rssi_dbm": -78, "rx_rate_mbps": 72.0},
        autoconfig_enabled=True,
        connectivity={"target_peer_ip": "192.168.137.42", "peer_reachable": True, "peer_rtt_ms": 22.0, "jitter_ms": 24.5, "loss_pct": 5.0, "host_internet_ok": True},
    )
    snap_bad["wifi_adapters"][0]["powersave_enabled"] = subopt
    rep_warn = classify_wireless_snapshot(snap_bad, cfg)
    codes = {f.code for l in rep_warn.layers for f in l.faults}
    assert {"WIFI_POWERSAVE_ENABLED", "WIFI_BAND_24GHZ", "WIFI_SIGNAL_WEAK", "WIFI_LINK_SLOW", "WIFI_BG_SCAN_ACTIVE", "WIFI_JITTER_HIGH"} <= codes

    # LAN mode AP Isolation detection + Switch skipping W6
    lan_cfg = AppConfig(active_transport="wireless", wireless_submode="lan", active_preset="switch", wireless_peer_ip="192.168.1.88")
    snap_iso = _base_wireless_snap(
        connectivity={"target_peer_ip": "192.168.1.88", "peer_reachable": False, "gateway_reachable": True, "host_internet_ok": True}
    )
    rep_iso = classify_wireless_snapshot(snap_iso, lan_cfg)
    w3 = next(l for l in rep_iso.layers if l.layer == "W3")
    w6 = next(l for l in rep_iso.layers if l.layer == "W6")
    assert any(f.code == "LAN_AP_ISOLATION_SUSPECTED" for f in w3.faults)
    assert w6.status == Severity.SKIPPED


def test_wireless_actions_guard_backup_and_restore(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig(active_transport="wireless", wireless_submode="hotspot")

    assert {"enable-hotspot", "disable-hotspot", "optimize-wifi", "restore-wifi"} <= set(SUPPORTED_ACTIONS)

    # 1. enable-hotspot blocked if Wired WireDeckNat active without force
    res_block = enable_hotspot(paths, cfg, force=False, snapshot_fn=lambda *a, **kw: _base_wireless_snap(wired_nat_active=True))
    assert res_block.ok is False and res_block.code == "WIRED_NAT_CONFLICT" and res_block.exit_code == 2

    # 2. enable-hotspot succeeds when forced and includes pre-configure stop guard when upgrading from 2.4GHz
    called: list[str] = []
    hs_scripts: list[str] = []

    def _hs_runner(sc: str, t: int) -> PsResult:
        hs_scripts.append(sc)
        return _ok_ps('{"state":"On","band":"FiveGigahertz"}')

    res_force = enable_hotspot(
        paths,
        cfg,
        force=True,
        runner=_hs_runner,
        snapshot_fn=lambda *a, **kw: _base_wireless_snap(wired_nat_active=True),
        disable_direct_fn=lambda: called.append("disabled") or res_block.__class__(ok=True, code="OK", message="OK"),
    )
    assert res_force.ok is True and called == ["disabled"]
    assert "$wasOn" in hs_scripts[-1] and "$origBand" in hs_scripts[-1]
    assert hs_scripts[-1].index("StopTetheringAsync") < hs_scripts[-1].index("ConfigureAccessPointAsync")

    # 3. optimize-wifi NEVER pauses autoconfig when disconnected!
    scripts_seen: list[str] = []

    def _capture_runner(sc: str, t: int) -> PsResult:
        scripts_seen.append(sc)
        return _ok_ps("OPTIMIZE_WIFI_OK\n")

    snap_disc = _base_wireless_snap(wlan={"connected": False}, autoconfig_enabled=True)
    snap_disc["wifi_adapters"][0]["advanced_properties"] = [
        {"keyword": "MIMOPowerSaveMode", "display_value": "自动 SMPS", "registry_value": "1"}
    ]
    res_disc = optimize_wifi(paths, cfg, runner=_capture_runner, snapshot_fn=lambda *a, **kw: snap_disc)
    assert res_disc.ok is True
    assert res_disc.details["paused_scan"] is False
    assert "autoconfig enabled=no" not in scripts_seen[-1]
    assert (paths.runtime_dir / "wifi_backup.json").is_file()

    # 4. Connected optimize DOES pause autoconfig + resolves ValidDisplayValues (Intel MIMOPowerSaveMode fallback='3'),
    # and restore-wifi restores properties + removes backup
    (paths.runtime_dir / "wifi_backup.json").unlink()
    snap_conn = _base_wireless_snap(wlan={"connected": True}, autoconfig_enabled=True)
    snap_conn["wifi_adapters"][0]["advanced_properties"] = [
        {"keyword": "MIMOPowerSaveMode", "display_value": "Auto SMPS", "registry_value": "0"},
        {"keyword": "*PacketCoalescing", "display_value": "Enabled", "registry_value": "1"},
    ]
    res_conn = optimize_wifi(paths, cfg, runner=_capture_runner, snapshot_fn=lambda *a, **kw: snap_conn)
    assert res_conn.ok is True
    assert res_conn.details["paused_scan"] is True
    assert "autoconfig enabled=no" in scripts_seen[-1]
    assert "ValidDisplayValues" in scripts_seen[-1]
    assert "MIMOPowerSaveMode" in scripts_seen[-1] and "'3'" in scripts_seen[-1]

    res_rest = restore_wifi(paths, cfg, runner=_capture_runner, snapshot_fn=lambda *a, **kw: snap_conn)
    assert res_rest.ok is True
    assert "autoconfig enabled=yes" in scripts_seen[-1]
    assert "RegistryValue '0'" in scripts_seen[-1]
    assert not (paths.runtime_dir / "wifi_backup.json").exists()

    # 5. disable-hotspot & action dispatch
    assert disable_hotspot(paths, cfg, runner=lambda sc, t: _ok_ps("DISABLE_HOTSPOT_OK")).ok is True
    res_disp = execute_action("optimize-wifi", paths, cfg, runner=_capture_runner, snapshot_fn=lambda *a, **kw: snap_conn)
    assert res_disp.ok is True


def test_wireless_config_isolation_privacy_and_bundled_toolkits(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = load_config(paths)
    cfg.active_transport = "wireless"
    cfg.wireless_submode = "lan"
    cfg.wireless_peer_ip = "192.168.1.99"
    cfg.switch_preset("playstation")
    cfg.wireless_peer_ip = "192.168.1.100"
    save_config(paths, cfg)

    reloaded = load_config(paths)
    assert reloaded.active_transport == "wireless"
    assert reloaded.wireless_submode == "lan"
    assert reloaded.wireless_peer_ip == "192.168.1.100"
    reloaded.switch_preset("xbox")
    assert reloaded.wireless_peer_ip == "192.168.1.99"

    # Privacy guard: passphrase must NEVER leak into exported bundle even if present in snapshot
    rep = classify_wireless_snapshot(_base_wireless_snap(passphrase="SuperSecretWifiKey123"), reloaded)
    bundle_path, prompt = export_diagnostic_bundle(paths, rep, reloaded)
    raw = bundle_path.read_text(encoding="utf-8")
    assert "SuperSecretWifiKey123" not in raw
    assert "--transport wireless" in prompt

    # 20 total bundled toolkits including all 4 wireless ones + run probe-wifi-advanced cleanly
    all_ids = {t.id for t in discover_all_toolkits()}
    assert len(all_ids) == 20
    assert {
        "common/probe-wifi-rf-scan",
        "common/probe-hotspot-clients",
        "common/bench-wifi-jitter",
        "common/probe-wifi-advanced",
    } <= all_ids
    tk = run_toolkit("common/probe-wifi-advanced", cfg=reloaded)
    assert tk.ok is True
    assert tk.data["tool"] == "common/probe-wifi-advanced"
