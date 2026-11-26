from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from wiredeck.bridge import LocalBridgeServer
from wiredeck.config import AppConfig
from wiredeck.diagnostics import classify_snapshot
from wiredeck.exporter import export_diagnostic_bundle, sanitize_payload
from wiredeck.extensions import (
    discover_extensions,
    run_extension,
    validate_extension_dir,
)
from wiredeck.models import ActionResult, Severity
from wiredeck.paths import resolve_app_paths


def _make_ext(ext_dir: Path, manifest: dict[str, Any], check_ps: str | None = None, fix_ps: str | None = None) -> Path:
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "ext.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if check_ps is not None:
        (ext_dir / "check.ps1").write_bytes(b"\xef\xbb\xbf" + check_ps.encode("utf-8"))
    if fix_ps is not None:
        (ext_dir / "fix.ps1").write_bytes(b"\xef\xbb\xbf" + fix_ps.encode("utf-8"))
    return ext_dir


def test_valid_extension_discovers_validates_and_runs(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    ext_dir = _make_ext(
        paths.extensions_dir / "demo-check",
        {
            "schema": "wiredeck-ext/v1",
            "id": "demo-check",
            "name": "示例检测插件",
            "author": "DSH",
            "enabled": True,
            "auto_check": True,
            "layer": "L1",
            "overrides_faults": ["NIC_POWERSAVE_ENABLED"],
            "requires_admin": False,
            "timeout_sec": 99,  # Must clamp to 15
            "check_script": "check.ps1",
            "fix_script": "fix.ps1",
            "description": "检测示例",
        },
        check_ps='Write-Output (\'{"status":"fail","code":"EXT_DEMO","title":"示例断点","message":"发现自定义异常","details":{"mode":"\' + $env:WIREDECK_EXT_MODE + \'"}}\')\r\n',
        fix_ps='Write-Output \'{"status":"ok","code":"FIXED","title":"已修复","message":"OK"}\'\r\n',
    )
    manifest, errors = validate_extension_dir(ext_dir)
    assert errors == []
    assert manifest is not None
    assert manifest.timeout_sec == 15

    found = discover_extensions(paths)
    assert [m.id for m in found] == ["demo-check"]

    res = run_extension(manifest, mode="check")
    assert res.status == Severity.FAIL
    assert res.code == "EXT_DEMO"
    assert res.fix_available is True
    assert res.overrides_faults == ["NIC_POWERSAVE_ENABLED"]


def test_extension_rejects_path_traversal(tmp_path: Path):
    ext_dir = _make_ext(
        tmp_path / "evil-ext",
        {
            "schema": "wiredeck-ext/v1",
            "id": "evil-ext",
            "name": "越界测试",
            "author": "test",
            "enabled": True,
            "auto_check": True,
            "layer": "L1",
            "overrides_faults": [],
            "requires_admin": False,
            "timeout_sec": 5,
            "check_script": "..\\..\\evil.ps1",
            "description": "traversal test",
        },
    )
    manifest, errors = validate_extension_dir(ext_dir)
    assert manifest is None
    assert any("escape" in e.lower() or "traversal" in e.lower() or "path" in e.lower() for e in errors)


def test_extension_timeout_and_invalid_json_emit_ext_error_without_crashing(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")

    slow_dir = _make_ext(
        paths.extensions_dir / "slow-ext",
        {
            "schema": "wiredeck-ext/v1",
            "id": "slow-ext",
            "name": "卡死插件",
            "author": "test",
            "enabled": True,
            "auto_check": True,
            "layer": "L2",
            "overrides_faults": [],
            "requires_admin": False,
            "timeout_sec": 1,
            "check_script": "check.ps1",
            "description": "sleeps forever",
        },
        check_ps="Start-Sleep -Seconds 30\r\n",
    )
    slow_m, errs = validate_extension_dir(slow_dir)
    assert errs == [] and slow_m is not None
    slow_res = run_extension(slow_m, mode="check")
    assert slow_res.status == Severity.WARN
    assert slow_res.code == "EXT_ERROR"

    bad_dir = _make_ext(
        paths.extensions_dir / "bad-json",
        {
            "schema": "wiredeck-ext/v1",
            "id": "bad-json",
            "name": "乱码输出插件",
            "author": "test",
            "enabled": True,
            "auto_check": True,
            "layer": "L3",
            "overrides_faults": [],
            "requires_admin": False,
            "timeout_sec": 5,
            "check_script": "check.ps1",
            "description": "invalid json",
        },
        check_ps='Write-Output "NOT A VALID JSON LINE"\r\n',
    )
    bad_m, errs2 = validate_extension_dir(bad_dir)
    assert errs2 == [] and bad_m is not None
    bad_res = run_extension(bad_m, mode="check")
    assert bad_res.status == Severity.WARN
    assert bad_res.code == "EXT_ERROR"


def test_sanitize_payload_redacts_username_and_secrets_and_export_bundle(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig()
    raw = {
        "path": r"C:\Users\alice_secret\AppData\Local\WireDeck\config.json",
        "unix_like": "c:/users/ALICE_SECRET/Downloads/x.exe",
        "api_token": "super-secret-token-123",
        "wifi_password": "my-wifi-password",
        "nested": [{"secret_key": "abc", "safe": "192.168.50.2"}],
    }
    clean = sanitize_payload(raw, username="alice_secret")
    assert r"C:\Users\<USER>" in clean["path"]
    assert "alice_secret" not in json.dumps(clean).lower()
    assert clean["api_token"] == "<REDACTED>"
    assert clean["wifi_password"] == "<REDACTED>"
    assert clean["nested"][0]["secret_key"] == "<REDACTED>"
    assert clean["nested"][0]["safe"] == "192.168.50.2"

    # Write sample log to verify inclusion
    (paths.logs_dir / "wiredeck_20261126.log").write_bytes(
        b"\xef\xbb\xbf[2026-11-26] C:\\Users\\alice_secret\\test\r\n"
    )
    report = classify_snapshot({"os_version": "Windows 11", "net_adapters": []}, cfg)
    bundle_path, prompt = export_diagnostic_bundle(paths, report, cfg, username="alice_secret")
    assert bundle_path.is_file()
    bundle_text = bundle_path.read_text(encoding="utf-8")
    assert "alice_secret" not in bundle_text.lower()
    assert "wiredeck-cli.exe" in prompt
    assert "extensions" in prompt
    assert str(bundle_path) in prompt


def test_local_bridge_server_auth_and_async_approval_lifecycle(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig()
    report = classify_snapshot({"os_version": "Windows 11", "net_adapters": []}, cfg)
    executed: list[str] = []

    def fake_action(action: str, opts: dict[str, Any]) -> ActionResult:
        executed.append(action)
        return ActionResult(ok=True, code="DONE", message=f"ran {action}")

    server = LocalBridgeServer(
        paths=paths,
        diagnose_fn=lambda: report,
        execute_action_fn=fake_action,
    )
    info = server.start()
    try:
        assert info.host == "127.0.0.1"
        assert info.port > 0
        assert len(info.token) == 64
        bridge_file = paths.runtime_dir / "bridge.json"
        assert bridge_file.is_file()

        # 1. Missing token -> 401
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"{info.url}/api/v1/diagnose", timeout=3)
        assert ei.value.code == 401

        headers = {"Authorization": f"Bearer {info.token}", "Content-Type": "application/json"}

        # 2. Valid token GET /api/v1/diagnose -> 200
        req = urllib.request.Request(f"{info.url}/api/v1/diagnose", headers=headers)
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["schema_version"] == "1.0"

        # 3. Valid token POST /api/v1/apply -> 202 pending_approval (NOT executed yet!)
        post_req = urllib.request.Request(
            f"{info.url}/api/v1/apply",
            data=json.dumps({"action": "enable-direct"}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(post_req, timeout=3) as resp:
            assert resp.status == 202
            job = json.loads(resp.read().decode("utf-8"))
            assert job["status"] == "pending_approval"
            job_id = job["job_id"]
        assert executed == []

        # 4. Approve job -> executes action and updates status to completed
        server.approve_job(job_id)
        assert executed == ["enable-direct"]
        job_req = urllib.request.Request(f"{info.url}/api/v1/jobs/{job_id}", headers=headers)
        with urllib.request.urlopen(job_req, timeout=3) as resp:
            jdata = json.loads(resp.read().decode("utf-8"))
            assert jdata["status"] == "completed"
            assert jdata["result"]["ok"] is True

        # 5. Reject second job -> does NOT execute
        post_req2 = urllib.request.Request(
            f"{info.url}/api/v1/apply",
            data=json.dumps({"action": "clear-netnat", "force": True}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(post_req2, timeout=3) as resp:
            job2_id = json.loads(resp.read().decode("utf-8"))["job_id"]
        server.reject_job(job2_id)
        assert executed == ["enable-direct"]
        job2_req = urllib.request.Request(f"{info.url}/api/v1/jobs/{job2_id}", headers=headers)
        with urllib.request.urlopen(job2_req, timeout=3) as resp:
            assert json.loads(resp.read().decode("utf-8"))["status"] == "rejected"
    finally:
        server.stop()

    assert not (paths.runtime_dir / "bridge.json").exists()
