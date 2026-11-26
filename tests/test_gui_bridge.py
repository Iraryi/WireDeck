from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from PIL import Image

from wiredeck.app import WireDeckApi, build_preview_report
from wiredeck.paths import resolve_app_paths
from wiredeck.version import APP_VERSION


def test_brand_assets_exist_and_ico_has_all_required_sizes():
    root = Path(__file__).resolve().parents[1]
    svg_path = root / "assets" / "logo.svg"
    ico_path = root / "assets" / "wiredeck.ico"
    png_path = root / "assets" / "logo.png"
    assert svg_path.is_file()
    assert "<svg" in svg_path.read_text(encoding="utf-8")
    assert png_path.is_file()
    assert ico_path.is_file()
    with Image.open(ico_path) as im:
        sizes = sorted(im.info.get("sizes", set()))
        assert sizes == [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def test_wiredeck_api_state_settings_export_and_bridge_toggle(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    api = WireDeckApi(paths=paths, snapshot_fn=lambda cfg, **kw: {"os_version": "Windows 11", "net_adapters": []})

    state = api.get_state()
    assert state["app_version"] == APP_VERSION
    assert state["config"]["console_profile"] == "xbox"
    assert state["bridge"]["running"] is False

    rep = api.diagnose()
    assert rep["schema_version"] == "1.0"
    assert len(rep["layers"]) == 6

    saved = api.save_settings({"language": "en", "console_profile": "ps5"})
    assert saved["ok"] is True
    assert api.get_state()["config"]["language"] == "en"
    assert api.get_state()["config"]["console_profile"] == "playstation"

    # Settings validation rejects invalid IP, duplicate IP, and cross-subnet IP
    assert api.save_settings({"host_ip": "999.1.1.1"})["ok"] is False
    assert api.save_settings({"host_ip": "192.168.50.2", "peer_ip": "192.168.50.2"})["code"] == "SAME_IP"
    assert api.save_settings({"host_ip": "192.168.50.2", "peer_ip": "10.0.0.1"})["code"] == "SUBNET_MISMATCH"

    exp = api.export_for_ai()
    assert exp["ok"] is True
    assert Path(exp["bundle_path"]).is_file()
    assert "wiredeck-cli.exe" in exp["prompt"]

    # Bridge toggle ON / OFF
    on_res = api.toggle_bridge(True)
    assert on_res["ok"] is True
    assert on_res["running"] is True
    assert on_res["url"].startswith("http://127.0.0.1:")
    assert len(on_res["token"]) == 64

    off_res = api.toggle_bridge(False)
    assert off_res["ok"] is True
    assert off_res["running"] is False

    # Sample extension creation for first-time AI onboarding
    sample = api.create_sample_extension()
    assert sample["ok"] is True
    assert sample["created"] is True
    listed = api.list_extensions()
    assert any(e["valid"] and e["manifest"]["id"] == "sample-dns-check" for e in listed)
    # Second click is idempotent
    sample2 = api.create_sample_extension()
    assert sample2["ok"] is True
    assert sample2["created"] is False


    # Startup onboarding with transport, per-preset isolated settings & built-in toolkits
    assert state["show_onboarding"] is True
    onb = api.complete_onboarding("ja", "switch", "wireless")
    assert onb["ok"] is True
    assert onb["config"]["onboarding_completed"] is True
    assert onb["config"]["language"] == "ja"
    assert onb["config"]["active_transport"] == "wireless"
    assert onb["config"]["active_preset"] == "switch"
    assert onb["config"]["peer_ip"] == "192.168.50.20"
    assert api.get_state()["show_onboarding"] is False

    # Switch back to wired cleanly and test wireless bridge APIs
    assert api.set_transport("wired")["active_transport"] == "wired"
    assert api.set_wireless_submode("lan")["wireless_submode"] == "lan"
    assert api.save_settings({"wireless_peer_ip": "bad-ip"})["ok"] is False
    assert api.save_settings({"wireless_peer_ip": "192.168.1.77"})["ok"] is True

    # Built-in preview wireless report
    w_api = WireDeckApi(paths=paths, preview_state="wireless")
    w_rep = w_api.diagnose_wireless()
    assert w_rep["transport"] == "wireless"
    assert w_rep["mode"] == "hotspot"
    creds = w_api.get_hotspot_credentials()
    assert creds["ok"] is True and creds["ssid"] == "WireDeck-5G"

    # Edit Switch peer_ip, switch to PlayStation, verify Switch setting persists independently
    assert api.save_settings({"peer_ip": "192.168.50.25", "dns_servers": "119.29.29.29, 8.8.8.8"})["ok"] is True
    sw = api.switch_preset("playstation")
    assert sw["config"]["peer_ip"] == "192.168.50.10"
    sw_back = api.switch_preset("switch")
    assert sw_back["config"]["peer_ip"] == "192.168.50.25"
    assert sw_back["config"]["dns_servers"] == ["119.29.29.29", "8.8.8.8"]

    # Built-in AI toolkits for active preset + common
    ps_tools = api.switch_preset("other")
    assert ps_tools["ok"] is True
    tools = api.list_toolkits()
    tool_ids = {t["id"] for t in tools}
    assert "common/probe-route-table" in tool_ids
    assert "other/emit-peer-config-snippets" in tool_ids
    assert "xbox/audit-uwp-loopback" not in tool_ids

    tool_run = api.run_toolkit("other/emit-peer-config-snippets")
    assert tool_run["ok"] is True
    assert "linux" in tool_run["data"] and "macos" in tool_run["data"]


def test_preview_states_are_deterministic_and_zero_pii():
    for mode in ("ready", "breakpoint", "wizard", "ai", "onboarding"):
        rep = build_preview_report(mode)
        d = rep.to_dict()
        raw = json.dumps(d, ensure_ascii=False)
        assert "65428" not in raw
        assert "PortableSSS" not in raw
        assert len(d["layers"]) == 6
    bp = build_preview_report("breakpoint")
    assert bp.primary_fault is not None
    assert bp.primary_fault.code == "VPN_TUN_ACTIVE"

    from wiredeck.app import build_wireless_preview_report

    for wmode in ("wireless", "wireless_lan", "wireless_wizard"):
        wrep = build_wireless_preview_report(wmode)
        wd = wrep.to_dict()
        wraw = json.dumps(wd, ensure_ascii=False)
        assert "65428" not in wraw
        assert "PortableSSS" not in wraw
        assert wd["transport"] == "wireless"
        assert len(wd["layers"]) == 6


def test_i18n_js_has_exact_key_parity_and_concise_home_copy():
    root = Path(__file__).resolve().parents[1]
    i18n_file = root / "src" / "wiredeck" / "web" / "i18n.js"
    assert i18n_file.is_file()
    content = i18n_file.read_text(encoding="utf-8")
    m = re.search(r"window\.WIREDECK_I18N\s*=\s*(\{.*\});", content, re.DOTALL)
    assert m is not None, "i18n.js must assign pure JSON object to window.WIREDECK_I18N"
    data: dict[str, dict[str, Any]] = json.loads(m.group(1))
    assert set(data.keys()) == {"zh-CN", "zh-TW", "en", "ja"}
    base_keys = set(data["zh-CN"].keys())
    for lang, d in data.items():
        assert set(d.keys()) == base_keys, f"Key mismatch for {lang}: {base_keys ^ set(d.keys())}"

    # Enforce concise home UI copy discipline
    for key in ("btn_enable_direct", "btn_disable_direct", "node_nic", "node_cable", "node_gate", "node_share", "node_console"):
        assert len(data["zh-CN"][key]) <= 12
