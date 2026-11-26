from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wiredeck.config import AppConfig, load_config, save_config
from wiredeck.diagnostics import classify_snapshot
from wiredeck.exporter import export_diagnostic_bundle
from wiredeck.models import Severity
from wiredeck.paths import resolve_app_paths
from wiredeck.presets import (
    ALL_PRESET_IDS,
    get_preset,
    list_presets,
    normalize_preset_id,
)
from wiredeck.toolkits import discover_toolkits, run_toolkit


def test_preset_registry_covers_five_presets_with_distinct_flows():
    ids = [p.id for p in list_presets()]
    assert ids == ["xbox", "playstation", "switch", "handheld", "other"]
    assert list(ALL_PRESET_IDS) == ids

    # Backwards-compatible aliases
    assert normalize_preset_id("ps5") == "playstation"
    assert normalize_preset_id("ps4") == "playstation"
    assert normalize_preset_id("generic") == "other"
    assert normalize_preset_id("unknown-box") == "xbox"

    xbox = get_preset("xbox")
    ps = get_preset("playstation")
    sw = get_preset("switch")
    hh = get_preset("handheld")
    other = get_preset("other")

    assert xbox.uses_uwp_loopback is True
    assert ps.uses_uwp_loopback is False
    assert sw.uses_uwp_loopback is False
    assert hh.uses_uwp_loopback is False
    assert other.uses_uwp_loopback is False

    assert "uwp" in xbox.wizard_steps
    assert "uwp" not in ps.wizard_steps
    assert "client" in ps.wizard_steps
    assert "client" not in sw.wizard_steps
    assert "client" in hh.wizard_steps
    assert other.wizard_steps == ("nic", "peer")


def test_per_preset_settings_are_isolated_while_shared_host_fields_persist(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = load_config(paths)
    assert cfg.onboarding_completed is False
    assert cfg.active_preset == "xbox"
    assert cfg.peer_ip == "192.168.50.1"

    # Customize Xbox preset
    cfg.peer_ip = "192.168.50.11"
    cfg.dns_servers = ["223.5.5.5", "8.8.8.8"]
    cfg.language = "ja"
    cfg.onboarding_completed = True
    save_config(paths, cfg)

    # Switch to PlayStation preset and customize independently
    cfg2 = load_config(paths)
    assert cfg2.language == "ja"
    assert cfg2.onboarding_completed is True
    cfg2.switch_preset("playstation")
    assert cfg2.active_preset == "playstation"
    assert cfg2.peer_ip != "192.168.50.11"  # Isolated from Xbox!
    cfg2.peer_ip = "192.168.50.22"
    cfg2.dns_servers = ["119.29.29.29", "8.8.8.8"]
    save_config(paths, cfg2)

    # Switch back to Xbox -> original Xbox IP & DNS are intact!
    cfg3 = load_config(paths)
    assert cfg3.peer_ip == "192.168.50.22"
    cfg3.switch_preset("xbox")
    assert cfg3.peer_ip == "192.168.50.11"
    assert cfg3.dns_servers == ["223.5.5.5", "8.8.8.8"]
    # Shared language stays ja
    assert cfg3.language == "ja"


def test_preset_specific_diagnostics_skip_irrelevant_checks():
    def _snap(endpoint_ok: bool, clients: dict[str, Any]) -> dict[str, Any]:
        return {
            "os_version": "Windows 11",
            "is_admin": False,
            "net_adapters": [
                {
                    "name": "Ethernet",
                    "interface_description": "2.5GbE",
                    "interface_index": 12,
                    "status": "Up",
                    "link_speed_bps": 1_000_000_000,
                    "link_speed_text": "1 Gbps",
                    "pnp_device_id": "PCI\\VEN_10EC",
                    "is_virtual": False,
                    "is_wireless": False,
                    "rx_errors": 0,
                    "tx_errors": 0,
                    "rx_discards": 0,
                    "tx_discards": 0,
                    "powersave_enabled": [],
                }
            ],
            "pnp_ethernet_devices": [],
            "ipv4_addresses": [
                {"interface_alias": "Ethernet", "interface_index": 12, "ip": "192.168.50.2", "prefix_length": 24}
            ],
            "default_routes": [],
            "ip_forwarding": {"Ethernet": True},
            "ics_sharing_interfaces": [],
            "shared_access_service_status": "Stopped",
            "udp_listeners": [],
            "net_nats": [{"name": "WireDeckNat", "internal_subnet": "192.168.50.0/24"}],
            "firewall_rules": {"WireDeck-WinNAT-Allow": True, "WireDeck-Direct-Allow": True},
            "uwp_exempt_packages": [],
            "uwp_missing_packages": ["microsoft.windowsstore_8wekyb3d8bbwe"],
            "stream_clients": clients,
            "connectivity": {
                "peer_reachable": True,
                "peer_rtt_ms": 0.4,
                "host_internet_ok": True,
                "xbox_live_ok": False,
                "service_endpoint_ok": endpoint_ok,
                "service_endpoint_status": 200 if endpoint_ok else 0,
            },
        }

    # PlayStation ignores UWP missing, checks PSN endpoint & Chiaki/PS Remote Play
    ps_cfg = AppConfig(console_profile="playstation")
    ps_rep = classify_snapshot(
        _snap(endpoint_ok=False, clients={"chiaki_path": None, "ps_remote_path": None}),
        ps_cfg,
    )
    l5_ps = next(l for l in ps_rep.layers if l.layer == "L5")
    l6_ps = next(l for l in ps_rep.layers if l.layer == "L6")
    assert any(f.code == "PSN_UNREACHABLE" for f in l5_ps.faults)
    assert not any(f.code == "UWP_LOOPBACK_MISSING" for f in l6_ps.faults)
    assert any(f.code == "PS_CLIENT_MISSING" for f in l6_ps.faults)

    # Switch ignores UWP & streaming clients completely, checks Nintendo endpoint
    sw_cfg = AppConfig(console_profile="switch")
    sw_rep = classify_snapshot(_snap(endpoint_ok=False, clients={}), sw_cfg)
    l5_sw = next(l for l in sw_rep.layers if l.layer == "L5")
    l6_sw = next(l for l in sw_rep.layers if l.layer == "L6")
    assert any(f.code == "NINTENDO_UNREACHABLE" for f in l5_sw.faults)
    assert l6_sw.status == Severity.SKIPPED

    # Handheld ignores UWP, detects Moonlight/Sunshine/Steam
    hh_cfg = AppConfig(console_profile="handheld")
    hh_rep = classify_snapshot(
        _snap(endpoint_ok=True, clients={"moonlight_path": "C:\\Moonlight.exe"}),
        hh_cfg,
    )
    l6_hh = next(l for l in hh_rep.layers if l.layer == "L6")
    assert l6_hh.status == Severity.OK


def test_bundled_toolkits_exist_for_common_and_every_preset_and_run_cleanly(tmp_path: Path):
    common_tools = discover_toolkits(preset_id=None, include_common=True)
    assert len(common_tools) >= 5
    common_ids = {t.id for t in common_tools}
    assert "common/probe-route-table" in common_ids
    assert "common/probe-udp-ports" in common_ids

    for pid in ALL_PRESET_IDS:
        preset_tools = discover_toolkits(preset_id=pid, include_common=False)
        assert len(preset_tools) >= 1, f"Missing preset-specific tools for {pid}"

    # Actually run a fast pure read-only common tool & preset tool to verify UTF-8 JSON output!
    res = run_toolkit("other/emit-peer-config-snippets", cfg=AppConfig(console_profile="other"))
    assert res.ok is True
    assert "192.168.50.1" in json.dumps(res.data)

    # Verify exporter embeds active preset & common toolkits into AI handoff prompt
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig(console_profile="playstation")
    rep = classify_snapshot({"os_version": "Windows 11", "net_adapters": []}, cfg)
    bundle_path, prompt = export_diagnostic_bundle(paths, rep, cfg)
    assert "toolkit run" in prompt
    assert "playstation/check-psn-endpoints" in prompt
    assert "common/probe-route-table" in prompt
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["active_preset"] == "playstation"
    assert len(bundle["available_toolkits"]) >= 6
