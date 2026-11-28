"""Tests for v0.2.3 power-ups: screenshot, link benchmark, controller battery/BT check, and config backup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiredeck import cli as cli_mod
from wiredeck.config import (
    BACKUP_SCHEMA_VERSION,
    AppConfig,
    RecorderSettings,
    export_config_backup,
    import_config_backup,
    load_config,
    save_config,
)
from wiredeck.diagnostics import evaluate_link_benchmark, run_link_benchmark
from wiredeck.paths import resolve_app_paths
from wiredeck.recorder import capture_screenshot
from wiredeck.system_env import detect_controllers, evaluate_system_health
from wiredeck.win32_helpers import Win32Ops


def test_config_backup_export_and_import_preserves_plugin_configs(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "local")
    cfg = AppConfig(active_preset="playstation", theme="dark", language="en")
    cfg.peer_ip = "192.168.60.1"
    save_config(paths, cfg)
    (paths.plugin_configs_dir / "framegen-sharpen.json").write_text(
        json.dumps({"enabled": True, "settings": {"sharpen_mode": "cas"}}),
        encoding="utf-8",
    )

    backup_file = tmp_path / "backups" / "my_bak.json"
    ex_res = export_config_backup(paths, backup_file, cfg=cfg)
    assert ex_res["ok"] is True
    assert ex_res["plugin_config_count"] == 1
    bak_json = json.loads(backup_file.read_text(encoding="utf-8"))
    assert bak_json["schema"] == BACKUP_SCHEMA_VERSION

    # Restore into a fresh installation with pre-existing settings
    paths2 = resolve_app_paths(tmp_path / "app2", local_appdata=tmp_path / "local2")
    save_config(paths2, AppConfig(active_preset="xbox"))
    im_res = import_config_backup(paths2, backup_file)
    assert im_res["ok"] is True
    assert im_res["restored_plugin_configs"] == 1
    reloaded = load_config(paths2)
    assert reloaded.active_preset == "playstation"
    assert reloaded.theme == "dark"
    assert reloaded.peer_ip == "192.168.60.1"
    assert any(paths2.data_dir.glob("config.json.pre_import.*"))
    restored_plg = json.loads((paths2.plugin_configs_dir / "framegen-sharpen.json").read_text(encoding="utf-8"))
    assert restored_plg["settings"]["sharpen_mode"] == "cas"

    # Bad schema rejected cleanly
    bad_bak = tmp_path / "bad.json"
    bad_bak.write_text('{"schema":"other/v1","config":{}}', encoding="utf-8")
    assert import_config_backup(paths2, bad_bak)["ok"] is False


def test_link_benchmark_grades_s_a_b_c_f():
    s = evaluate_link_benchmark("192.168.50.1", [0.4, 0.5, 0.4, 0.6], transport="wired")
    assert s["ok"] is True and s["grade"] == "S" and s["loss_pct"] == 0.0

    a = evaluate_link_benchmark("192.168.137.42", [3.0, 5.0, 4.0, 6.0], transport="wireless")
    assert a["ok"] is True and a["grade"] == "A"

    b = evaluate_link_benchmark("192.168.1.88", [12.0, 22.0, 14.0, 18.0], transport="wireless")
    assert b["ok"] is True and b["grade"] == "B"

    c = evaluate_link_benchmark("192.168.1.88", [35.0, None, 18.0, 42.0], transport="wireless")
    assert c["ok"] is True and c["grade"] == "C" and c["loss_pct"] == 25.0

    f = evaluate_link_benchmark("192.168.50.1", [None, None, None, None], transport="wired")
    assert f["ok"] is False and f["grade"] == "F"

    live_res = run_link_benchmark("192.168.50.1", count=6, ping_fn=lambda ip, ms: (True, 0.8))
    assert live_res["ok"] is True and live_res["sent"] == 6 and live_res["grade"] == "S"


def test_controller_battery_and_24g_bt_coexistence_health_warnings():
    ctrl = detect_controllers(
        xinput_probe=lambda: [{"slot": 0, "connection": "wireless", "battery_level": "low"}],
        bt_names=["Xbox Wireless Controller"],
    )
    assert ctrl["count"] == 1
    assert ctrl["low_battery_slots"] == [0]
    assert ctrl["has_bluetooth_gamepad"] is True

    health = evaluate_system_health(
        system_env={"on_battery": False, "controllers": ctrl},
        cfg_dict={"recorder": {}},
        recorder_detected={"effective_backend": "obs", "obs": {}},
        target_status={"online": True, "free_gb": 50},
        effective_split={"mode": "off"},
        wifi_band_24g=True,
    )
    codes = {w["code"] for w in health["warnings"]}
    assert "CONTROLLER_LOW_BATTERY" in codes
    assert "BT_24G_COEXISTENCE" in codes
    ctrl_item = next(it for it in health["items"] if it["id"] == "controller")
    assert ctrl_item["ok"] is False


def test_pure_window_screenshot_wakes_and_sequences_pngs(tmp_path: Path):
    usb_dir = tmp_path / "USB_Clips"
    usb_dir.mkdir()
    wins = [
        {"hwnd": 999, "title": "XStreaming", "process_name": "XStreaming.exe", "class_name": "Chrome_WidgetWin_1", "is_minimized": True}
    ]
    woken: list[int] = []
    captured_hwnds: list[int] = []
    ops = Win32Ops(list_windows=lambda: wins, restore_window_no_activate=lambda h: woken.append(h) or True, sleep=lambda _s: None)
    cfg = AppConfig(active_preset="xbox", recorder=RecorderSettings(output_dir=str(usb_dir), capture_scope="stream_window"))

    def fake_hwnd_cap(hwnd: int, out_p: Path) -> bool:
        captured_hwnds.append(hwnd)
        out_p.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
        return True

    r1 = capture_screenshot(cfg, win32_ops=ops, capture_hwnd_fn=fake_hwnd_cap)
    r2 = capture_screenshot(cfg, win32_ops=ops, capture_hwnd_fn=fake_hwnd_cap)
    assert r1.ok and r2.ok
    assert woken == [999, 999]
    assert captured_hwnds == [999, 999]
    assert Path(r1.details["path"]).name.startswith("1_")
    assert Path(r2.details["path"]).name.startswith("2_")
    assert Path(r1.details["path"]).parent == usb_dir / "Screenshots"

    # Strict guard blocks if window closed
    cfg_strict = AppConfig(recorder=RecorderSettings(output_dir=str(usb_dir), capture_scope="stream_window", strict_window_only=True))
    r_blk = capture_screenshot(cfg_strict, win32_ops=Win32Ops(list_windows=lambda: [], sleep=lambda _s: None))
    assert r_blk.ok is False and r_blk.code == "CAPTURE_TARGET_MISSING"


def test_cli_benchmark_and_config_export_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    fake_paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "local")
    save_config(fake_paths, AppConfig(active_preset="switch"))
    monkeypatch.setattr(cli_mod, "resolve_app_paths", lambda: fake_paths)

    bak_p = tmp_path / "cli_bak.json"
    assert cli_mod.main(["config", "export", str(bak_p), "--json"]) == 0
    exp_out = json.loads(capsys.readouterr().out)
    assert exp_out["ok"] is True

    assert cli_mod.main(["config", "import", str(bak_p), "--json"]) == 0
    imp_out = json.loads(capsys.readouterr().out)
    assert imp_out["ok"] is True and imp_out["config"]["active_preset"] == "switch"

    monkeypatch.setattr(cli_mod, "run_link_benchmark", lambda ip, count=12, transport="wired": {"ok": True, "target_ip": ip, "grade": "S", "avg_ms": 0.4, "jitter_ms": 0.1, "loss_pct": 0.0, "recommended_bitrate": "50+ Mbps"})
    assert cli_mod.main(["benchmark", "--json"]) == 0
    bm_out = json.loads(capsys.readouterr().out)
    assert bm_out["grade"] == "S"
