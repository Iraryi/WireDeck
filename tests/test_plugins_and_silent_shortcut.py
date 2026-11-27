from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from wiredeck.app import WireDeckApi, _run_quick_play_detailed
from wiredeck.app import main as app_main
from wiredeck.cli import main as cli_main
from wiredeck.config import AppConfig
from wiredeck.models import ActionResult
from wiredeck.paths import resolve_app_paths
from wiredeck.plugins import (
    emit_plugin_hook,
    fetch_plugin_catalog,
    install_plugin_from_github,
    list_installed_plugins,
    load_plugin_config,
    run_plugin_check,
    save_plugin_config,
    set_plugin_enabled,
    uninstall_plugin,
    validate_plugin_dir,
)
from wiredeck.ps_runner import run_powershell
from wiredeck.stream_client import (
    build_play_shortcut_name,
    create_play_shortcut,
    resolve_stream_client_info,
)
from wiredeck.version import NAT_NAME, PLUGIN_SCHEMA_VERSION
from wiredeck.win32_helpers import Win32Ops, parse_hotkey


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().lower()


def _fake_resp_cls():
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

    return _FakeResp


def _make_sample_plugin(
    plugin_dir: Path,
    *,
    plugin_id: str = "demo-addon",
    version: str = "1.0.0",
    hooks: dict[str, str] | None = None,
    scripts: dict[str, str] | None = None,
) -> Path:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": PLUGIN_SCHEMA_VERSION,
        "id": plugin_id,
        "name": f"Demo {plugin_id}",
        "version": version,
        "author": "Test",
        "category": "automation",
        "description": "Test plugin",
        "hooks": hooks or {"after_client_launch": "hooks/after.ps1"},
        "settings_schema": [
            {"key": "hotkey", "type": "hotkey", "label": "Hotkey", "default": "Ctrl+Alt+S"},
            {"key": "auto_run", "type": "bool", "label": "Auto Run", "default": True},
        ],
    }
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for rel, body in (scripts or {"hooks/after.ps1": 'Write-Output \'{"ok":true,"status":"ok","code":"OK","message":"done"}\'\n'}).items():
        p = plugin_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    return plugin_dir


def test_parse_hotkey_tokens_valid_and_invalid():
    assert parse_hotkey("Ctrl+Alt+S") == [0x11, 0x12, ord("S")]
    assert parse_hotkey("Win+Shift+F12") == [0x5B, 0x10, 0x7B]
    assert parse_hotkey("Alt+Enter") == [0x12, 0x0D]
    with pytest.raises(ValueError):
        parse_hotkey("")
    with pytest.raises(ValueError):
        parse_hotkey("Ctrl+UnknownKey99")


def test_app_identical_silent_shortcut_properties(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    fake_xs = tmp_path / "Apps" / "XStreaming.exe"
    fake_xs.parent.mkdir(parents=True, exist_ok=True)
    fake_xs.write_bytes(b"MZ")

    cfg = AppConfig(language="zh-CN", active_preset="xbox", active_transport="wired", custom_client_path=str(fake_xs))
    info = resolve_stream_client_info("xbox", str(fake_xs))
    assert info["name"] == "XStreaming"
    assert info["icon_location"] == f"{fake_xs},0"
    assert build_play_shortcut_name(cfg, client_info=info) == "XStreaming.lnk"

    desk = tmp_path / "desktop"
    res = create_play_shortcut(paths, cfg, desktop_dir=desk, client_info=info)
    assert res.ok is True
    lnk_file = desk / "XStreaming.lnk"
    assert lnk_file.is_file()

    # Inspect actual Windows .lnk COM properties
    quoted_lnk = "'" + str(lnk_file).replace("'", "''") + "'"
    inspect_res = run_powershell(
        f"$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut({quoted_lnk}); "
        "$out = @{ Arguments = $s.Arguments; IconLocation = $s.IconLocation; WindowStyle = $s.WindowStyle }; "
        "$out | ConvertTo-Json -Compress",
        timeout=15,
    )
    assert inspect_res.returncode == 0
    meta = json.loads(inspect_res.stdout.strip())
    assert "--quick-play" in meta["Arguments"]
    assert "--silent" in meta["Arguments"]
    assert str(fake_xs) in meta["IconLocation"]
    assert int(meta["WindowStyle"]) == 7


def test_silent_quick_play_never_opens_gui_and_runs_exit_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    _make_sample_plugin(
        paths.plugins_dir / "exit-watcher",
        plugin_id="exit-watcher",
        hooks={
            "on_client_exit": "hooks/on_exit.ps1",
        },
        scripts={
            "hooks/on_exit.ps1": 'Write-Output \'{"ok":true,"status":"ok","code":"EXITED","host_commands":[{"cmd":"notify","title":"WireDeck","message":"ended"}]}\'\n',
        },
    )

    events_seen: list[str] = []
    notifications: list[tuple[str, str]] = []
    waited_pids: list[int] = []

    fake_ops = Win32Ops(
        show_notification=lambda title, msg: notifications.append((title, msg)) or True,
        wait_for_pid_exit=lambda pid, poll_interval=1.0: waited_pids.append(pid) or True,
    )

    # Mock WireDeckApi in app.main so no real WebView2 or real network is touched
    import wiredeck.app as app_mod

    webview_called = {"count": 0}
    monkeypatch.setitem(sys.modules, "webview", type("FakeWebview", (), {"create_window": lambda **kw: webview_called.__setitem__("count", 99)})())

    # Case 1: Success + silent + on_client_exit hook -> runs exit hook and returns 0 without WebView2
    api_ok = WireDeckApi(
        paths=paths,
        snapshot_fn=lambda *a, **kw: {
            "net_adapters": [{"name": "Ethernet", "status": "Up", "link_speed_bps": 1_000_000_000, "is_virtual": False, "is_wireless": False, "powersave_enabled": []}],
            "pnp_ethernet_devices": [],
            "ipv4_addresses": [{"interface_alias": "Ethernet", "interface_index": 12, "ip": "192.168.50.2", "prefix_length": 24}],
            "default_routes": [{"interface_alias": "Wi-Fi", "interface_index": 8, "next_hop": "192.168.1.1", "metric": 35, "is_virtual_or_tun": False}],
            "ip_forwarding": {"Ethernet": True},
            "ics_sharing_interfaces": [],
            "udp_listeners": [],
            "net_nats": [{"name": NAT_NAME, "internal_subnet": "192.168.50.0/24"}],
            "firewall_rules": {},
            "uwp_missing_packages": [],
            "stream_clients": {"xstreaming_path": "C:\\XStreaming.exe"},
            "connectivity": {"peer_reachable": True},
        },
        win32_ops=fake_ops,
    )
    monkeypatch.setattr(app_mod, "WireDeckApi", lambda **kw: api_ok)
    monkeypatch.setattr(
        app_mod,
        "_run_quick_play_detailed",
        lambda *a, **kw: (events_seen.append("qp") or True, "", "", {"pid": 4321, "exe_name": "XStreaming.exe", "client_name": "XStreaming"}),
    )
    rc_ok = app_main(["--quick-play", "--silent"])
    assert rc_ok == 0
    assert webview_called["count"] == 0
    assert waited_pids == [4321]
    assert ("WireDeck", "ended") in notifications

    # Case 2: Failure + silent -> shows tray notification, writes quick_play.log, returns 1 WITHOUT GUI
    notifications.clear()
    monkeypatch.setattr(
        app_mod,
        "_run_quick_play_detailed",
        lambda *a, **kw: (False, "home", "未检测到千兆网线", {}),
    )
    rc_fail = app_main(["--quick-play", "--silent"])
    assert rc_fail == 1
    assert webview_called["count"] == 0
    assert notifications == [("WireDeck", "未检测到千兆网线")]
    assert (paths.logs_dir / "quick_play.log").is_file()


def test_quick_play_executes_lifecycle_hooks_in_order_and_dispatches_host_commands(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    marker_file = tmp_path / "hook_order.txt"
    q_marker = str(marker_file).replace("'", "''")

    _make_sample_plugin(
        paths.plugins_dir / "order-tracker",
        plugin_id="order-tracker",
        hooks={
            "before_quick_play": "hooks/bqp.ps1",
            "before_transport_enable": "hooks/bte.ps1",
            "after_transport_enable": "hooks/ate.ps1",
            "before_client_launch": "hooks/bcl.ps1",
            "after_client_launch": "hooks/acl.ps1",
            "after_quick_play": "hooks/aqp.ps1",
        },
        scripts={
            "hooks/bqp.ps1": f"Add-Content -LiteralPath '{q_marker}' -Value 'before_quick_play' -Encoding UTF8; Write-Output '{{\"ok\":true}}'\n",
            "hooks/bte.ps1": f"Add-Content -LiteralPath '{q_marker}' -Value 'before_transport_enable' -Encoding UTF8; Write-Output '{{\"ok\":true}}'\n",
            "hooks/ate.ps1": f"Add-Content -LiteralPath '{q_marker}' -Value 'after_transport_enable' -Encoding UTF8; Write-Output '{{\"ok\":true}}'\n",
            "hooks/bcl.ps1": f"Add-Content -LiteralPath '{q_marker}' -Value 'before_client_launch' -Encoding UTF8; Write-Output '{{\"ok\":true,\"host_commands\":[{{\"cmd\":\"launch_process\",\"exe\":\"C:\\\\LS.exe\",\"minimized\":true}}]}}'\n",
            "hooks/acl.ps1": f"Add-Content -LiteralPath '{q_marker}' -Value 'after_client_launch' -Encoding UTF8; Write-Output '{{\"ok\":true,\"host_commands\":[{{\"cmd\":\"wait_window\",\"timeout_sec\":5}},{{\"cmd\":\"sleep\",\"seconds\":0.2}},{{\"cmd\":\"focus_window\"}},{{\"cmd\":\"send_hotkey\",\"keys\":\"Ctrl+Alt+S\"}}]}}'\n",
            "hooks/aqp.ps1": f"Add-Content -LiteralPath '{q_marker}' -Value 'after_quick_play' -Encoding UTF8; Write-Output '{{\"ok\":true}}'\n",
        },
    )

    ops_trace: list[tuple[str, Any]] = []
    fake_ops = Win32Ops(
        is_process_running=lambda name: False,
        launch_process=lambda exe, args=None, cwd="", minimized=False: ops_trace.append(("launch", exe, minimized)) or 888,
        wait_window=lambda **kw: ops_trace.append(("wait", kw.get("pid"))) or 999001,
        sleep=lambda sec: ops_trace.append(("sleep", sec)),
        focus_window=lambda hwnd: ops_trace.append(("focus", hwnd)) or True,
        send_hotkey=lambda keys: ops_trace.append(("hotkey", keys)) or True,
    )

    idle_snap: dict[str, Any] = {
        "os_version": "Windows 11",
        "is_admin": False,
        "net_adapters": [{"name": "Ethernet", "interface_description": "2.5GbE", "interface_index": 12, "status": "Up", "link_speed_bps": 1_000_000_000, "is_virtual": False, "is_wireless": False, "powersave_enabled": []}],
        "pnp_ethernet_devices": [],
        "ipv4_addresses": [{"interface_alias": "Ethernet", "interface_index": 12, "ip": "192.168.50.2", "prefix_length": 24}],
        "default_routes": [{"interface_alias": "Wi-Fi", "interface_index": 8, "next_hop": "192.168.1.1", "metric": 35, "is_virtual_or_tun": False}],
        "ip_forwarding": {"Ethernet": True, "Wi-Fi": True},
        "ics_sharing_interfaces": [],
        "udp_listeners": [],
        "net_nats": [],
        "firewall_rules": {},
        "uwp_missing_packages": [],
        "stream_clients": {"xstreaming_path": "C:\\XStreaming.exe"},
        "connectivity": {"peer_reachable": True},
    }

    api = WireDeckApi(paths=paths, snapshot_fn=lambda *a, **kw: idle_snap, win32_ops=fake_ops)
    api._execute_or_elevate = lambda act, opts: ActionResult(ok=True, code="OK", message="OK")  # type: ignore[method-assign]

    ok, view, err, details = _run_quick_play_detailed(
        api,
        preset_id="xbox",
        transport="wired",
        submode="hotspot",
        launch_fn=lambda preset_id="xbox": ActionResult(
            ok=True,
            code="LAUNCHED",
            message="OK",
            details={"pid": 7777, "exe_name": "XStreaming.exe", "client_name": "XStreaming"},
        ),
        silent=True,
        win32_ops=fake_ops,
    )
    assert ok is True and view == "" and err == "" and details["pid"] == 7777

    order = [line.strip() for line in marker_file.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    assert order == [
        "before_quick_play",
        "before_transport_enable",
        "after_transport_enable",
        "before_client_launch",
        "after_client_launch",
        "after_quick_play",
    ]
    assert ops_trace == [
        ("launch", "C:\\LS.exe", True),
        ("wait", 7777),
        ("sleep", 0.2),
        ("focus", 999001),
        ("hotkey", "Ctrl+Alt+S"),
    ]


def test_plugin_security_validation_and_timeout_isolation(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")

    # 1. Path traversal rejected
    evil = _make_sample_plugin(
        tmp_path / "evil",
        plugin_id="evil-traversal",
        hooks={"after_client_launch": "..\\..\\evil.ps1"},
        scripts={},
    )
    m_evil, errs_evil = validate_plugin_dir(evil)
    assert m_evil is None
    assert any("traversal" in e.lower() or "escape" in e.lower() for e in errs_evil)

    # 2. Invalid schema rejected
    bad_schema = _make_sample_plugin(tmp_path / "bad_schema", plugin_id="bad-schema")
    pjson = json.loads((bad_schema / "plugin.json").read_text(encoding="utf-8"))
    pjson["schema"] = "unknown/v99"
    (bad_schema / "plugin.json").write_text(json.dumps(pjson), encoding="utf-8")
    m_bad, errs_bad = validate_plugin_dir(bad_schema)
    assert m_bad is None and any("schema" in e.lower() for e in errs_bad)

    # 3. Timeout & invalid JSON do not crash emit_plugin_hook
    slow_dir = _make_sample_plugin(
        paths.plugins_dir / "slow-plugin",
        plugin_id="slow-plugin",
        scripts={"hooks/after.ps1": "Start-Sleep -Seconds 25\n"},
    )
    pj = json.loads((slow_dir / "plugin.json").read_text(encoding="utf-8"))
    pj["timeout_sec"] = 1
    (slow_dir / "plugin.json").write_text(json.dumps(pj), encoding="utf-8")

    results = emit_plugin_hook(paths, "after_client_launch", {"preset": "xbox"})
    assert len(results) == 1
    assert results[0].code == "PLUGIN_TIMEOUT"
    assert results[0].ok is False


def test_github_catalog_sha256_install_update_and_config_preservation(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    repo_root = Path(__file__).resolve().parents[1]
    index_path = repo_root / "registry" / "index.json"
    assert index_path.is_file(), "Run scripts/build_registry.py before tests"

    index_bytes = index_path.read_bytes()
    url_map: dict[str, bytes] = {
        "https://raw.githubusercontent.com/Iraryi/WireDeck/main/registry/index.json": index_bytes,
    }
    plugin_src = repo_root / "registry" / "plugins" / "framegen-sharpen"
    for fpath in plugin_src.rglob("*"):
        if fpath.is_file():
            rel = fpath.relative_to(plugin_src).as_posix()
            url_map[f"https://raw.githubusercontent.com/Iraryi/WireDeck/main/registry/plugins/framegen-sharpen/{rel}"] = fpath.read_bytes()

    FakeResp = _fake_resp_cls()

    def fake_urlopen(req, timeout=12.0):
        u = req.full_url
        if u not in url_map:
            raise FileNotFoundError(u)
        return FakeResp(url_map[u])

    # 1. Fetch catalog & install cleanly
    cat = fetch_plugin_catalog(urlopen_fn=fake_urlopen)
    assert cat["ok"] is True
    assert any(p["id"] == "framegen-sharpen" for p in cat["plugins"])

    res = install_plugin_from_github(paths, plugin_id="framegen-sharpen", urlopen_fn=fake_urlopen)
    assert res.ok is True and res.code == "PLUGIN_INSTALLED"
    assert (paths.plugins_dir / "framegen-sharpen" / "plugin.json").is_file()

    # Save custom user setting before simulating update/reinstall
    save_plugin_config(paths, "framegen-sharpen", settings={"scale_hotkey": "Ctrl+Alt+F10", "delay_sec": 3.5})
    res2 = install_plugin_from_github(paths, plugin_id="framegen-sharpen", urlopen_fn=fake_urlopen)
    assert res2.ok is True
    manifest, errs = validate_plugin_dir(paths.plugins_dir / "framegen-sharpen")
    assert manifest is not None and not errs
    reloaded_cfg = load_plugin_config(paths, manifest)
    assert reloaded_cfg["settings"]["scale_hotkey"] == "Ctrl+Alt+F10"
    assert reloaded_cfg["settings"]["delay_sec"] == 3.5

    # 2. Tampered file hash -> install fails cleanly without corrupting installed plugin
    bad_index = json.loads(index_bytes.decode("utf-8"))
    bad_index["plugins"][0]["files"][0]["sha256"] = "0" * 64
    url_map["https://example.invalid/bad_index.json"] = json.dumps(bad_index).encode("utf-8")
    res_bad = install_plugin_from_github(
        paths,
        plugin_id="framegen-sharpen",
        registry_url="https://example.invalid/bad_index.json",
        urlopen_fn=fake_urlopen,
    )
    assert res_bad.ok is False and res_bad.code == "PLUGIN_INSTALL_FAILED"
    assert (paths.plugins_dir / "framegen-sharpen" / "plugin.json").is_file()

    # 3. Uninstall preserves config unless remove_config=True
    rm_res = uninstall_plugin(paths, "framegen-sharpen", remove_config=False)
    assert rm_res.ok is True
    assert not (paths.plugins_dir / "framegen-sharpen").exists()
    assert (paths.plugin_configs_dir / "framegen-sharpen.json").is_file()


def test_framegen_sharpen_real_hooks_and_cli_plugin_subcommands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    repo_root = Path(__file__).resolve().parents[1]
    src_plugin = repo_root / "registry" / "plugins" / "framegen-sharpen"
    dst_plugin = paths.plugins_dir / "framegen-sharpen"

    import shutil

    shutil.copytree(src_plugin, dst_plugin)
    fake_ls = tmp_path / "Steam" / "LosslessScaling.exe"
    fake_ls.parent.mkdir(parents=True, exist_ok=True)
    fake_ls.write_bytes(b"MZ")

    save_plugin_config(
        paths,
        "framegen-sharpen",
        enabled=True,
        settings={
            "engine": "lossless_scaling",
            "exe_path": str(fake_ls),
            "auto_launch_engine": True,
            "auto_trigger_scale": True,
            "scale_hotkey": "",
            "delay_sec": 1.0,
            "enabled_presets": ["xbox"],
            "auto_close_on_exit": True,
        },
    )

    events: list[tuple[Any, ...]] = []
    fake_ops = Win32Ops(
        is_process_running=lambda name: False,
        launch_process=lambda exe, args=None, cwd="", minimized=False: events.append(("launch", exe, minimized)) or 111,
        wait_window=lambda **kw: events.append(("wait", kw.get("pid"))) or 222,
        sleep=lambda sec: events.append(("sleep", sec)),
        focus_window=lambda hwnd: events.append(("focus", hwnd)) or True,
        send_hotkey=lambda keys: events.append(("hotkey", keys)) or True,
        stop_process=lambda proc: events.append(("stop", proc)) or True,
    )

    ctx = {
        "preset": "xbox",
        "transport": "wired",
        "submode": "hotspot",
        "client_pid": 5050,
        "client_exe": "XStreaming.exe",
        "client_name": "XStreaming",
    }
    r1 = emit_plugin_hook(paths, "before_client_launch", ctx, win32_ops=fake_ops)
    r2 = emit_plugin_hook(paths, "after_client_launch", ctx, win32_ops=fake_ops)
    r3 = emit_plugin_hook(paths, "on_client_exit", ctx, win32_ops=fake_ops)
    assert r1[0].ok and r2[0].ok and r3[0].ok
    assert events == [
        ("launch", str(fake_ls), True),
        ("wait", 5050),
        ("sleep", 1.0),
        ("focus", 222),
        ("hotkey", "Ctrl+Alt+S"),
        ("stop", "LosslessScaling.exe"),
    ]

    # Diagnostic check succeeds for valid fake_ls path
    chk = run_plugin_check(paths, "framegen-sharpen", ctx)
    assert chk["ok"] is True and chk["code"] == "ENGINE_READY"

    # CLI plugin list/disable/enable/remove
    import wiredeck.cli as cli_mod

    monkeypatch.setattr(cli_mod, "resolve_app_paths", lambda *a, **kw: paths)
    assert cli_main(["plugin", "list", "--json"]) == 0
    assert cli_main(["plugin", "disable", "framegen-sharpen", "--json"]) == 0
    assert list_installed_plugins(paths)[0]["enabled"] is False
    assert cli_main(["plugin", "enable", "framegen-sharpen", "--json"]) == 0
    assert list_installed_plugins(paths)[0]["enabled"] is True
    assert cli_main(["plugin", "remove", "framegen-sharpen", "--purge", "--json"]) == 0
    assert list_installed_plugins(paths) == []
