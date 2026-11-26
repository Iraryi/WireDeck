from __future__ import annotations

import hashlib
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from wiredeck.actions import execute_action
from wiredeck.config import AppConfig
from wiredeck.paths import resolve_app_paths
from wiredeck.ps_runner import PsResult
from wiredeck.stream_client import (
    download_and_verify_asset,
    fetch_latest_xstreaming_release,
)
from wiredeck.version import FW_RULE_NAT, NAT_NAME


def _base_snapshot(
    *,
    tun_active: bool = False,
    ics_active: bool = False,
    foreign_nat: str | None = None,
    third_party_53: bool = False,
    pcie_disabled: bool = False,
) -> dict[str, Any]:
    default_routes = [
        {
            "interface_alias": "WLAN",
            "interface_index": 8,
            "next_hop": "192.168.8.1",
            "metric": 35,
            "is_virtual_or_tun": False,
            "interface_description": "Intel Wi-Fi 6E",
        }
    ]
    if tun_active:
        default_routes.append(
            {
                "interface_alias": "LightXtreme",
                "interface_index": 33,
                "next_hop": "172.19.0.2",
                "metric": 5,
                "is_virtual_or_tun": True,
                "interface_description": "sing-tun Tunnel",
            }
        )

    adapters = [
        {
            "name": "以太网",
            "interface_description": "Realtek Gaming 2.5GbE",
            "interface_index": 12,
            "status": "Up",
            "link_speed_bps": 1_000_000_000,
            "link_speed_text": "1 Gbps",
            "pnp_device_id": "PCI\\VEN_10EC&DEV_8125\\4&1234",
            "is_virtual": False,
            "is_wireless": False,
            "rx_errors": 0,
            "tx_errors": 0,
            "rx_discards": 0,
            "tx_discards": 0,
            "powersave_enabled": ["*EEE", "GigaLite"],
        },
        {
            "name": "WLAN",
            "interface_description": "Intel Wi-Fi 6E",
            "interface_index": 8,
            "status": "Up",
            "link_speed_bps": 866_000_000,
            "link_speed_text": "866 Mbps",
            "pnp_device_id": "PCI\\VEN_8086&DEV_51F1\\3&1",
            "is_virtual": False,
            "is_wireless": True,
            "rx_errors": 0,
            "tx_errors": 0,
            "rx_discards": 0,
            "tx_discards": 0,
            "powersave_enabled": [],
        },
    ]
    pnp_devices = []
    if pcie_disabled:
        adapters = [adapters[1]]
        pnp_devices = [
            {
                "friendly_name": "Realtek Gaming 2.5GbE",
                "instance_id": "PCI\\VEN_10EC&DEV_8125\\4&1234",
                "status": "Unknown",
                "problem_code": 45,
                "parent_instance_id": "PCI\\VEN_8086&DEV_7AB0\\3&1",
                "parent_friendly_name": "Intel PCIe Root Port #15",
                "parent_problem_code": 22,
                "parent_status": "Error",
            }
        ]

    udp_listeners = []
    if third_party_53:
        udp_listeners.append(
            {"port": 53, "pid": 8888, "process_name": "SmartDNS", "service_name": "SmartDNS"}
        )

    net_nats = []
    if foreign_nat:
        net_nats.append({"name": foreign_nat, "internal_subnet": "172.24.0.0/20"})

    return {
        "os_version": "Windows 11",
        "is_admin": True,
        "net_adapters": adapters,
        "pnp_ethernet_devices": pnp_devices,
        "ipv4_addresses": [
            {"interface_alias": "以太网", "interface_index": 12, "ip": "169.254.1.2", "prefix_length": 16},
            {"interface_alias": "WLAN", "interface_index": 8, "ip": "192.168.8.104", "prefix_length": 24},
        ],
        "default_routes": default_routes,
        "ip_forwarding": {"以太网": False, "WLAN": False},
        "ics_sharing_interfaces": [{"name": "WLAN", "sharing_type": 0}] if ics_active else [],
        "shared_access_service_status": "Running",
        "udp_listeners": udp_listeners,
        "net_nats": net_nats,
        "firewall_rules": {FW_RULE_NAT: False, "WireDeck-Direct-Allow": False},
        "uwp_exempt_packages": [],
        "uwp_missing_packages": [],
        "stream_clients": {"xstreaming_path": None, "xbox_app_installed": False},
        "connectivity": {"peer_reachable": True, "peer_rtt_ms": 0.4, "host_internet_ok": True, "xbox_live_ok": True},
    }


def test_enable_direct_blocked_by_vpn_tun(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig()
    calls: list[str] = []

    def fake_runner(script: str, timeout: int = 15) -> PsResult:
        calls.append(script)
        return PsResult(0, "", "")

    res = execute_action(
        "enable-direct",
        paths,
        cfg,
        runner=fake_runner,
        snapshot_fn=lambda *a, **kw: _base_snapshot(tun_active=True),
    )
    assert res.ok is False
    assert res.exit_code == 2
    assert res.code == "VPN_TUN_ACTIVE"
    assert calls == []


def test_enable_direct_blocked_by_ics(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig()
    res = execute_action(
        "enable-direct",
        paths,
        cfg,
        runner=lambda s, t=15: PsResult(0, "", ""),
        snapshot_fn=lambda *a, **kw: _base_snapshot(ics_active=True),
    )
    assert res.ok is False
    assert res.exit_code == 2
    assert res.code == "ICS_ACTIVE"


def test_enable_direct_writes_nic_backup_once_and_configures_winnat_with_bom_log(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig()
    scripts: list[str] = []

    def fake_runner(script: str, timeout: int = 30) -> PsResult:
        scripts.append(script)
        return PsResult(0, '{"dhcp": true, "ips": ["169.254.1.2/16"]}', "")

    # Even with third-party non-ICS UDP 53 warning, enable-direct MUST proceed!
    res = execute_action(
        "enable-direct",
        paths,
        cfg,
        runner=fake_runner,
        snapshot_fn=lambda *a, **kw: _base_snapshot(third_party_53=True),
    )
    assert res.ok is True
    assert res.exit_code == 0

    backup_file = paths.runtime_dir / "nic_backup.json"
    assert backup_file.is_file()
    first_backup = backup_file.read_text(encoding="utf-8")

    joined = "\n".join(scripts)
    assert NAT_NAME in joined
    assert FW_RULE_NAT in joined
    assert "Forwarding Enabled" in joined
    assert "192.168.50.2" in joined
    assert "SharedAccess" not in joined

    # Check UTF-8 BOM + CRLF log file written
    assert res.log_file is not None
    raw_log = Path(res.log_file).read_bytes()
    assert raw_log.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw_log

    # Second run must NOT overwrite existing nic_backup.json
    backup_file.write_text('{"marker": "keep-original"}', encoding="utf-8")
    res2 = execute_action(
        "enable-direct",
        paths,
        cfg,
        runner=fake_runner,
        snapshot_fn=lambda *a, **kw: _base_snapshot(),
    )
    assert res2.ok is True
    assert backup_file.read_text(encoding="utf-8") == '{"marker": "keep-original"}'
    assert first_backup != ""


def test_disable_direct_preserves_static_ip_and_does_not_touch_third_party_forwarding(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig()
    scripts: list[str] = []

    snap = _base_snapshot()
    # Third-party VPN TUN & Hyper-V adapters have Forwarding=True; WireDeck MUST NOT touch them!
    snap["ip_forwarding"] = {
        "以太网": True,
        "WLAN": True,
        "LightXtreme": True,
        "vEthernet (WSL)": True,
    }

    res = execute_action(
        "disable-direct",
        paths,
        cfg,
        runner=lambda s, t=30: (scripts.append(s) or PsResult(0, "OK", "")),
        snapshot_fn=lambda *a, **kw: snap,
    )
    assert res.ok is True
    joined = "\n".join(scripts)
    assert NAT_NAME in joined
    assert FW_RULE_NAT in joined
    assert "Forwarding Disabled" in joined
    assert "Dhcp Enabled" not in joined
    assert "LightXtreme" not in joined
    assert "vEthernet (WSL)" not in joined


def test_reset_nic_uses_valid_ps_param_restores_static_and_cleans_uplink_forwarding(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig()
    scripts: list[str] = []
    (paths.runtime_dir / "nic_backup.json").write_text("{broken", encoding="utf-8")

    res = execute_action(
        "reset-nic",
        paths,
        cfg,
        runner=lambda s, t=30: (scripts.append(s) or PsResult(0, "OK", "")),
        snapshot_fn=lambda *a, **kw: _base_snapshot(),
    )
    assert res.ok is True
    joined = "\n".join(scripts)
    assert "Dhcp Enabled" in joined
    assert "-ResetServerAddresses" in joined
    assert "-ResetAddresses" not in joined
    assert "WLAN" in joined and "Forwarding Disabled" in joined

    # Also verify restoring non-DHCP static backup when valid backup exists
    scripts2: list[str] = []
    (paths.runtime_dir / "nic_backup.json").write_text(
        json.dumps({"interface_alias": "以太网", "dhcp": False, "ips": ["10.0.0.5/24"], "dns": ["1.1.1.1"]}),
        encoding="utf-8",
    )
    res2 = execute_action(
        "reset-nic",
        paths,
        cfg,
        runner=lambda s, t=30: (scripts2.append(s) or PsResult(0, "OK", "")),
        snapshot_fn=lambda *a, **kw: _base_snapshot(),
    )
    assert res2.ok is True
    joined2 = "\n".join(scripts2)
    assert "10.0.0.5" in joined2
    assert "1.1.1.1" in joined2


def test_enable_direct_has_atomic_rollback_on_winnat_failure_and_honors_l3_extension(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig()
    scripts: list[str] = []

    res = execute_action(
        "enable-direct",
        paths,
        cfg,
        runner=lambda s, t=30: (scripts.append(s) or PsResult(0, "ENABLE_DIRECT_OK", "")),
        snapshot_fn=lambda *a, **kw: _base_snapshot(),
    )
    assert res.ok is True
    joined = "\n".join(scripts)
    assert "try {" in joined and "catch {" in joined and "Forwarding Disabled" in joined
    assert (paths.runtime_dir / "direct_state.json").is_file()

    # Add an L3 auto_check extension returning fail -> must block enable-direct!
    ext_dir = paths.extensions_dir / "l3-blocker"
    ext_dir.mkdir(parents=True)
    (ext_dir / "ext.json").write_text(
        json.dumps(
            {
                "schema": "wiredeck-ext/v1",
                "id": "l3-blocker",
                "name": "自定义门禁",
                "author": "DSH",
                "enabled": True,
                "auto_check": True,
                "layer": "L3",
                "overrides_faults": [],
                "requires_admin": False,
                "timeout_sec": 5,
                "check_script": "check.ps1",
                "description": "blocks direct",
            }
        ),
        encoding="utf-8",
    )
    (ext_dir / "check.ps1").write_text(
        'Write-Output \'{"status":"fail","code":"EXT_L3_BLOCK","title":"扩展门禁","message":"自定义冲突阻止直连"}\'\n',
        encoding="utf-8",
    )
    res_blocked = execute_action(
        "enable-direct",
        paths,
        cfg,
        runner=lambda s, t=30: PsResult(0, "OK", ""),
        snapshot_fn=lambda *a, **kw: _base_snapshot(),
    )
    assert res_blocked.ok is False
    assert res_blocked.exit_code == 2
    assert res_blocked.code == "EXT_L3_BLOCK"


def test_real_powershell_cmdlet_parameter_contract_and_quiet_progress():
    from wiredeck.ps_runner import run_powershell

    res = run_powershell(
        "$cmd = Get-Command Set-DnsClientServerAddress; "
        "$cmd.Parameters.ContainsKey('ResetServerAddresses')",
        timeout=10,
    )
    assert res.returncode == 0
    assert "True" in res.stdout
    assert "#< CLIXML" not in res.stderr


def test_launch_stream_client_returns_missing_when_neither_installed(monkeypatch: pytest.MonkeyPatch):
    from wiredeck import stream_client

    monkeypatch.setattr(
        stream_client,
        "detect_stream_clients",
        lambda: {"xstreaming_path": None, "xbox_app_installed": False},
    )
    res = stream_client.launch_stream_client()
    assert res.ok is False
    assert res.code == "STREAM_CLIENT_MISSING"


def test_clear_netnat_requires_force_and_clear_ics_never_kills_third_party(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig()

    # Without force -> refuse
    res_no_force = execute_action(
        "clear-netnat",
        paths,
        cfg,
        force=False,
        runner=lambda s, t=30: PsResult(0, "", ""),
        snapshot_fn=lambda *a, **kw: _base_snapshot(foreign_nat="DockerNAT"),
    )
    assert res_no_force.ok is False
    assert res_no_force.exit_code == 2
    assert res_no_force.code == "CONFIRM_REQUIRED"
    assert res_no_force.details["nat_names"] == ["DockerNAT"]

    # With force -> removes foreign NAT
    scripts: list[str] = []
    res_force = execute_action(
        "clear-netnat",
        paths,
        cfg,
        force=True,
        runner=lambda s, t=30: (scripts.append(s) or PsResult(0, "", "")),
        snapshot_fn=lambda *a, **kw: _base_snapshot(foreign_nat="DockerNAT"),
    )
    assert res_force.ok is True
    assert "DockerNAT" in "\n".join(scripts)

    # clear-ics with third-party SmartDNS on UDP 53 must NOT kill SmartDNS
    ics_scripts: list[str] = []
    res_ics = execute_action(
        "clear-ics",
        paths,
        cfg,
        runner=lambda s, t=30: (ics_scripts.append(s) or PsResult(0, "", "")),
        snapshot_fn=lambda *a, **kw: _base_snapshot(third_party_53=True),
    )
    assert res_ics.ok is True
    ics_joined = "\n".join(ics_scripts)
    assert "SmartDNS" not in ics_joined
    assert "Stop-Process" not in ics_joined


def test_fix_nic_enables_pcie_parent_and_disables_powersave(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig()
    scripts: list[str] = []

    res = execute_action(
        "fix-nic",
        paths,
        cfg,
        runner=lambda s, t=30: (scripts.append(s) or PsResult(0, "", "")),
        snapshot_fn=lambda *a, **kw: _base_snapshot(pcie_disabled=True),
    )
    assert res.ok is True
    joined = "\n".join(scripts)
    assert "PCI\\VEN_8086&DEV_7AB0\\3&1" in joined
    assert "Enable-PnpDevice" in joined
    assert "*EEE" in joined


def test_fix_uwp_exempts_core_packages(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig()
    scripts: list[str] = []

    res = execute_action(
        "fix-uwp",
        paths,
        cfg,
        runner=lambda s, t=30: (scripts.append(s) or PsResult(0, "", "")),
        snapshot_fn=lambda *a, **kw: _base_snapshot(),
    )
    assert res.ok is True
    joined = "\n".join(scripts)
    assert "CheckNetIsolation.exe" in joined
    assert "microsoft.windowsstore_8wekyb3d8bbwe" in joined
    assert "microsoft.gamingapp_8wekyb3d8bbwe" in joined


def test_download_and_verify_asset_checks_sha256_and_deletes_on_mismatch(tmp_path: Path):
    payload = b"wiredeck-test-binary-payload-12345"
    good_sha = hashlib.sha256(payload).hexdigest()
    bad_sha = "0" * 64

    class _FakeResp:
        def __init__(self, data: bytes):
            self._data = data
            self._pos = 0
            self.headers = {"Content-Length": str(len(data))}

        def read(self, n: int = -1) -> bytes:
            if self._pos >= len(self._data):
                return b""
            chunk = self._data[self._pos : self._pos + (n if n > 0 else len(self._data))]
            self._pos += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    dest_ok = tmp_path / "XStreaming-Setup.exe"
    got = download_and_verify_asset(
        "https://example.invalid/XStreaming-Setup.exe",
        dest_ok,
        good_sha,
        urlopen_fn=lambda req, timeout=30: _FakeResp(payload),
    )
    assert got == dest_ok
    assert dest_ok.read_bytes() == payload

    dest_bad = tmp_path / "XStreaming-Corrupt.exe"
    with pytest.raises(ValueError, match="SHA-256"):
        download_and_verify_asset(
            "https://example.invalid/XStreaming-Setup.exe",
            dest_bad,
            bad_sha,
            urlopen_fn=lambda req, timeout=30: _FakeResp(payload),
        )
    assert not dest_bad.exists()


def test_fetch_latest_xstreaming_release_handles_github_500_gracefully():
    def _fail_500(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

    info = fetch_latest_xstreaming_release(urlopen_fn=_fail_500)
    assert info.ok is False
    assert "500" in info.error or "GitHub" in info.error
    assert info.release_page_url.startswith("https://github.com/")


def test_create_play_shortcut_writes_real_lnk_inside_target_dir(tmp_path: Path):
    from wiredeck.config import AppConfig
    from wiredeck.paths import resolve_app_paths
    from wiredeck.stream_client import build_play_shortcut_name, create_play_shortcut

    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig(language="zh-CN", active_preset="xbox", active_transport="wired")
    desk = tmp_path / "desktop"
    res = create_play_shortcut(paths, cfg, desktop_dir=desk)
    assert res.ok is True
    assert res.code == "SHORTCUT_CREATED"
    expected_name = build_play_shortcut_name(cfg)
    lnk_file = desk / expected_name
    assert lnk_file.is_file() and lnk_file.stat().st_size > 100
