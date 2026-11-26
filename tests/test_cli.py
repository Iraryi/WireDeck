from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from wiredeck import cli
from wiredeck.models import ActionResult
from wiredeck.paths import resolve_app_paths
from wiredeck.ps_runner import PsResult


def _mock_snapshot(tun_active: bool = False) -> dict[str, Any]:
    routes = [
        {
            "interface_alias": "WLAN",
            "interface_index": 8,
            "next_hop": "192.168.8.1",
            "metric": 35,
            "is_virtual_or_tun": False,
            "interface_description": "Intel Wi-Fi",
        }
    ]
    if tun_active:
        routes.append(
            {
                "interface_alias": "LightXtreme",
                "interface_index": 33,
                "next_hop": "172.19.0.2",
                "metric": 5,
                "is_virtual_or_tun": True,
                "interface_description": "sing-tun",
            }
        )
    return {
        "os_version": "Windows 11",
        "is_admin": False,
        "net_adapters": [
            {
                "name": "以太网",
                "interface_description": "Realtek 2.5GbE",
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
            },
            {
                "name": "WLAN",
                "interface_description": "Intel Wi-Fi",
                "interface_index": 8,
                "status": "Up",
                "link_speed_bps": 866_000_000,
                "link_speed_text": "866 Mbps",
                "pnp_device_id": "PCI\\VEN_8086",
                "is_virtual": False,
                "is_wireless": True,
                "rx_errors": 0,
                "tx_errors": 0,
                "rx_discards": 0,
                "tx_discards": 0,
                "powersave_enabled": [],
            },
        ],
        "pnp_ethernet_devices": [],
        "ipv4_addresses": [
            {"interface_alias": "以太网", "interface_index": 12, "ip": "192.168.50.2", "prefix_length": 24}
        ],
        "default_routes": routes,
        "ip_forwarding": {"以太网": False, "WLAN": False},
        "ics_sharing_interfaces": [],
        "shared_access_service_status": "Running",
        "udp_listeners": [],
        "net_nats": [],
        "firewall_rules": {"WireDeck-WinNAT-Allow": False, "WireDeck-Direct-Allow": False},
        "uwp_exempt_packages": [],
        "uwp_missing_packages": [],
        "stream_clients": {"xstreaming_path": None, "xbox_app_installed": True},
        "connectivity": {"peer_reachable": True, "peer_rtt_ms": 0.4, "host_internet_ok": True, "xbox_live_ok": True},
    }


def test_cli_does_not_import_webview():
    cli_path = Path(cli.__file__)
    tree = ast.parse(cli_path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "webview" not in imported


def test_cli_diagnose_and_status_json(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    monkeypatch.setattr(cli, "resolve_app_paths", lambda *a, **kw: paths)
    monkeypatch.setattr(cli, "collect_system_snapshot", lambda *a, **kw: _mock_snapshot())

    rc = cli.main(["diagnose", "--json", "--no-extensions"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "1.0"
    assert len(payload["layers"]) == 6
    assert payload["can_enable_direct"] is True

    rc2 = cli.main(["status", "--json"])
    assert rc2 == 0
    st = json.loads(capsys.readouterr().out)
    assert st["mode"] == "normal"
    assert st["can_enable_direct"] is True


def test_cli_apply_non_admin_without_elevate_exits_3_immediately(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    monkeypatch.setattr(cli, "resolve_app_paths", lambda *a, **kw: paths)
    monkeypatch.setattr(cli, "is_admin", lambda: False)
    elevate_called = []
    monkeypatch.setattr(
        cli,
        "run_elevated_worker",
        lambda *a, **kw: (elevate_called.append(1) or ActionResult(ok=True, code="OK", message="ok")),
    )

    rc = cli.main(["apply", "enable-direct", "--json"])
    assert rc == 3
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["code"] == "ADMIN_REQUIRED"
    assert elevate_called == []

    # With --elevate, delegates to run_elevated_worker
    rc2 = cli.main(["apply", "enable-direct", "--elevate", "--json"])
    assert rc2 == 0
    assert len(elevate_called) == 1


def test_cli_apply_admin_gate_blocked_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    monkeypatch.setattr(cli, "resolve_app_paths", lambda *a, **kw: paths)
    monkeypatch.setattr(cli, "is_admin", lambda: True)
    monkeypatch.setattr(cli, "collect_system_snapshot", lambda *a, **kw: _mock_snapshot(tun_active=True))

    rc = cli.main(["apply", "enable-direct", "--json"])
    assert rc == 2
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["code"] == "VPN_TUN_ACTIVE"


def test_cli_ext_list_validate_and_export_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    monkeypatch.setattr(cli, "resolve_app_paths", lambda *a, **kw: paths)
    monkeypatch.setattr(cli, "collect_system_snapshot", lambda *a, **kw: _mock_snapshot())

    ext_dir = paths.extensions_dir / "sample-cli-ext"
    ext_dir.mkdir(parents=True)
    (ext_dir / "ext.json").write_text(
        json.dumps(
            {
                "schema": "wiredeck-ext/v1",
                "id": "sample-cli-ext",
                "name": "CLI测试插件",
                "author": "DSH",
                "enabled": True,
                "auto_check": False,
                "layer": "L1",
                "overrides_faults": [],
                "requires_admin": False,
                "timeout_sec": 5,
                "check_script": "check.ps1",
                "description": "demo",
            }
        ),
        encoding="utf-8",
    )
    (ext_dir / "check.ps1").write_text('Write-Output \'{"status":"ok","code":"OK","title":"OK","message":"ok"}\'\n', encoding="utf-8")

    assert cli.main(["ext", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["extensions"][0]["manifest"]["id"] == "sample-cli-ext"

    assert cli.main(["ext", "validate", str(ext_dir), "--json"]) == 0
    val = json.loads(capsys.readouterr().out)
    assert val["valid"] is True

    assert cli.main(["export-bundle", "--json"]) == 0
    exp = json.loads(capsys.readouterr().out)
    assert exp["ok"] is True
    assert Path(exp["bundle_path"]).is_file()

    # check-wifi-tuned fast-path + export-bundle --transport wireless
    assert cli.main(["status", "--check-wifi-tuned"]) == 1
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    (paths.runtime_dir / "wifi_backup.json").write_text('{"interface_alias":"WLAN"}\n', encoding="utf-8")
    assert cli.main(["status", "--check-wifi-tuned"]) == 0

    from wiredeck import wireless_probes

    monkeypatch.setattr(
        wireless_probes,
        "collect_wireless_snapshot",
        lambda *a, **kw: {
            "is_admin": False,
            "wifi_adapters": [{"name": "WLAN", "status": "Up", "powersave_enabled": []}],
            "wlan": {"connected": True, "band": "5 GHz", "band_ghz": 5.0, "channel": 149, "rssi_dbm": -50, "signal_pct": 88},
            "autoconfig_enabled": False,
            "ipv4_addresses": [{"interface_alias": "WLAN", "ip": "192.168.1.50", "prefix_length": 24}],
            "wifi_gateway_ip": "192.168.1.1",
            "default_routes": [],
            "wired_nat_active": False,
            "hotspot": {"state": "On", "band": "FiveGigahertz", "ssid": "WireDeck-5G"},
            "hotspot_clients": [],
            "uwp_exempt_packages": [],
            "uwp_missing_packages": [],
            "stream_clients": {},
            "connectivity": {"host_internet_ok": True},
        },
    )
    w_bundle = tmp_path / "wireless_bundle.json"
    assert cli.main(["export-bundle", "--transport", "wireless", "--out", str(w_bundle), "--json"]) == 0
    w_exp = json.loads(capsys.readouterr().out)
    assert "--transport wireless" in w_exp["prompt"]
    assert json.loads(w_bundle.read_text(encoding="utf-8"))["report"]["transport"] == "wireless"


def test_cli_worker_executes_request_and_preserves_invoking_user_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    admin_paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "AdminB_AppData")
    user_paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "StandardUser_AppData")
    monkeypatch.setattr(cli, "resolve_app_paths", lambda *a, **kw: admin_paths)
    monkeypatch.setattr(cli, "collect_system_snapshot", lambda *a, **kw: _mock_snapshot())
    monkeypatch.setattr(cli, "run_powershell", lambda script, timeout=30: PsResult(0, "OK", ""))

    # Place extension ONLY in invoking standard user's data_dir (not AdminB's!)
    ext_dir = user_paths.extensions_dir / "user-only-ext"
    ext_dir.mkdir(parents=True)
    (ext_dir / "ext.json").write_text(
        json.dumps(
            {
                "schema": "wiredeck-ext/v1",
                "id": "user-only-ext",
                "name": "用户目录扩展",
                "author": "DSH",
                "enabled": True,
                "auto_check": False,
                "layer": "L1",
                "overrides_faults": [],
                "requires_admin": True,
                "timeout_sec": 5,
                "fix_script": "fix.ps1",
                "description": "demo",
            }
        ),
        encoding="utf-8",
    )
    (ext_dir / "fix.ps1").write_text(
        'Write-Output \'{"status":"ok","code":"USER_EXT_OK","title":"OK","message":"ran from user dir"}\'\n',
        encoding="utf-8",
    )

    req_path = user_paths.runtime_dir / "req_test.json"
    out_path = user_paths.runtime_dir / "res_test.json"
    req_path.write_text(
        json.dumps(
            {
                "action": "run-ext",
                "ext_id": "user-only-ext",
                "force": False,
                "data_dir": str(user_paths.data_dir),
            }
        ),
        encoding="utf-8",
    )

    rc = cli.main(["--worker", str(req_path), "--out", str(out_path)])
    assert rc == 0
    assert out_path.is_file()
    res = json.loads(out_path.read_text(encoding="utf-8"))
    assert res["ok"] is True
    assert res["code"] == "USER_EXT_OK"
    assert list(user_paths.logs_dir.glob("wiredeck_*.log"))

