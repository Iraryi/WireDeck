from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiredeck.config import AppConfig, load_config, save_config
from wiredeck.models import (
    ActionResult,
    DiagnosticReport,
    ExtensionManifest,
    ExtensionRunResult,
    FaultItem,
    LayerReport,
    Severity,
)
from wiredeck.paths import ensure_within, resolve_app_paths
from wiredeck.version import APP_ID, APP_NAME, APP_VERSION, SCHEMA_VERSION


def test_version_constants_are_single_source():
    assert APP_NAME == "WireDeck"
    assert APP_VERSION == "0.1.0"
    assert APP_ID == "{8E4F7A21-6C3D-4B9E-9A12-5D8C1F4E2B70}"
    assert SCHEMA_VERSION == "1.0"


def test_portable_mode_uses_local_data_dir_with_non_ascii_and_spaces(tmp_path: Path):
    app_dir = tmp_path / "测试 目录" / "WireDeck"
    app_dir.mkdir(parents=True)
    (app_dir / "portable.mode").write_text("", encoding="utf-8")
    p = resolve_app_paths(app_dir, local_appdata=tmp_path / "LocalAppData")
    assert p.is_portable is True
    assert p.data_dir == (app_dir / "data").resolve()
    assert p.config_file == p.data_dir / "config.json"
    assert p.extensions_dir == p.data_dir / "extensions"
    assert p.logs_dir == p.data_dir / "logs"
    assert p.exports_dir == p.data_dir / "exports"
    assert p.runtime_dir == p.data_dir / "runtime"
    assert p.extensions_dir.is_dir()
    assert p.logs_dir.is_dir()
    assert p.exports_dir.is_dir()
    assert p.runtime_dir.is_dir()


def test_standard_mode_uses_local_appdata_with_non_ascii(tmp_path: Path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    la = tmp_path / "用户 数据"
    p = resolve_app_paths(app_dir, local_appdata=la)
    assert p.is_portable is False
    assert p.data_dir == (la / "WireDeck").resolve()
    assert p.data_dir.is_dir()


def test_ensure_within_rejects_traversal_and_allows_nested(tmp_path: Path):
    root = tmp_path / "ext"
    root.mkdir()
    inside = root / "sub" / "check.ps1"
    inside.parent.mkdir()
    inside.write_text("Write-Output '{}'", encoding="utf-8")
    assert ensure_within(root, inside) == inside.resolve()

    with pytest.raises(ValueError):
        ensure_within(root, root / ".." / "evil.ps1")

    with pytest.raises(ValueError):
        ensure_within(root, Path("C:/Windows/System32/cmd.exe"))


def test_config_roundtrip_and_corrupt_recovery(tmp_path: Path):
    app_dir = tmp_path / "测试 目录" / "app"
    app_dir.mkdir(parents=True)
    p = resolve_app_paths(app_dir, local_appdata=tmp_path / "用户 数据")
    cfg = load_config(p)
    assert cfg.language == "zh-CN"
    assert cfg.console_profile == "xbox"
    assert cfg.host_ip == "192.168.50.2"
    assert cfg.prefix_length == 24
    assert cfg.peer_ip == "192.168.50.1"
    assert cfg.dns_servers == ["223.5.5.5", "8.8.8.8"]
    assert cfg.http_bridge_enabled is False

    cfg.language = "en"
    cfg.console_profile = "ps5"
    cfg.ethernet_interface = "以太网"
    save_config(p, cfg)
    reloaded = load_config(p)
    assert reloaded.language == "en"
    assert reloaded.console_profile == "playstation"
    assert reloaded.ethernet_interface == "以太网"

    # Corrupt config recovery backs up broken file and returns clean defaults
    p.config_file.write_text("{corrupt json", encoding="utf-8")
    recovered = load_config(p)
    assert recovered.console_profile == "xbox"
    assert recovered.host_ip == "192.168.50.2"
    backups = list(p.data_dir.glob("config.json.corrupt.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{corrupt json"


def test_config_ignores_unknown_keys_and_normalizes_invalid_profile(tmp_path: Path):
    p = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    p.config_file.write_text(
        json.dumps({"console_profile": "dreamcast", "unknown_future_key": 123}),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.console_profile == "xbox"


def test_models_serialize_cleanly():
    fault = FaultItem(
        code="VPN_TUN_ACTIVE",
        layer="L3",
        severity=Severity.FAIL,
        title="VPN 冲突",
        message="请先断开 VPN，再开启直连",
        source="builtin",
        fix_action=None,
    )
    layer = LayerReport(
        layer="L3",
        name="门禁",
        status=Severity.FAIL,
        summary="VPN 在线",
        faults=[fault],
    )
    rep = DiagnosticReport(
        schema_version=SCHEMA_VERSION,
        app_version=APP_VERSION,
        timestamp="2026-11-26T12:00:00+08:00",
        console_profile="xbox",
        mode="blocked",
        can_enable_direct=False,
        headline="检测到 VPN 冲突",
        primary_fault=fault,
        layers=[layer],
        snapshot={},
        extensions=[],
    )
    d = rep.to_dict()
    assert d["schema_version"] == "1.0"
    assert d["layers"][0]["status"] == "fail"
    assert d["primary_fault"]["code"] == "VPN_TUN_ACTIVE"

    res = ActionResult(ok=False, code="ADMIN_REQUIRED", message="需要管理员权限", exit_code=3)
    assert res.to_dict()["exit_code"] == 3

    manifest = ExtensionManifest(
        schema="wiredeck-ext/v1",
        id="sample-ext",
        name="示例扩展",
        author="DSH",
        enabled=True,
        auto_check=True,
        layer="L1",
        overrides_faults=[],
        requires_admin=False,
        timeout_sec=5,
        check_script="check.ps1",
        fix_script=None,
        description="demo",
        ext_dir=Path("."),
    )
    run_res = ExtensionRunResult(
        ext_id="sample-ext",
        name="示例扩展",
        author="DSH",
        layer="L1",
        status=Severity.OK,
        code="OK",
        title="正常",
        message="一切正常",
    )
    assert manifest.to_dict()["id"] == "sample-ext"
    assert run_res.to_dict()["status"] == "ok"
