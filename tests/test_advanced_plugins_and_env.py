from __future__ import annotations

import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from wiredeck.app import WireDeckApi, _parse_plugin_opt_args, _run_quick_play_detailed
from wiredeck.models import ActionResult
from wiredeck.paths import resolve_app_paths
from wiredeck.plugins import (
    emit_plugin_hook,
    fetch_plugin_catalog,
    install_plugin_from_github,
    resolve_plugin_dynamic_options,
    run_plugin_action,
    run_plugin_check,
    save_plugin_config,
    validate_plugin_dir,
)
from wiredeck.system_env import normalize_nvidia_driver_version, supports_nvidia_smooth_motion
from wiredeck.version import NAT_NAME
from wiredeck.win32_helpers import Win32Ops, download_vendor_file

SAMPLE_LS_XML = """<?xml version="1.0" encoding="utf-8"?>
<Settings xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Hotkey>S</Hotkey>
  <HotkeyModifierKeys>Alt Control</HotkeyModifierKeys>
  <GameProfiles>
    <Profile>
      <Title>默认</Title>
      <Path />
      <AutoScale>false</AutoScale>
      <ScalingMode>Custom</ScalingMode>
      <ScalingFitMode>AspectRatio</ScalingFitMode>
      <ScaleFactor>1</ScaleFactor>
      <ResizeBeforeScaling>true</ResizeBeforeScaling>
      <ScalingType>Integer</ScalingType>
      <LS1Type>BALANCED</LS1Type>
      <LS1Sharpness>1</LS1Sharpness>
      <LSFG2Mode>X2</LSFG2Mode>
      <LSFG3Mode1>FIXED</LSFG3Mode1>
      <LSFG3Multiplier>2</LSFG3Multiplier>
      <LSFG3Target>240</LSFG3Target>
      <FrameGeneration>Off</FrameGeneration>
      <CaptureApi>DXGI</CaptureApi>
      <MaxFrameLatency>1</MaxFrameLatency>
      <SyncMode>OFF</SyncMode>
    </Profile>
    <Profile>
      <Title>XStreamingAuto</Title>
      <Path>C:\\Games\\XStreaming\\XStreaming.exe</Path>
      <AutoScale>true</AutoScale>
      <ScalingType>LS1</ScalingType>
      <FrameGeneration>LSFG3</FrameGeneration>
      <CaptureApi>WGC</CaptureApi>
    </Profile>
  </GameProfiles>
</Settings>
"""


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


def test_nvidia_version_normalization_and_smooth_motion_detection():
    assert normalize_nvidia_driver_version("32.0.15.7284") == "572.84"
    assert normalize_nvidia_driver_version("32.0.16.2140") == "621.40"
    assert normalize_nvidia_driver_version("572.84") == "572.84"

    assert supports_nvidia_smooth_motion("NVIDIA GeForce RTX 5070 Ti Laptop GPU") is True
    assert supports_nvidia_smooth_motion("NVIDIA GeForce RTX 4060 Laptop GPU") is True
    assert supports_nvidia_smooth_motion("NVIDIA GeForce RTX 3060") is False
    assert supports_nvidia_smooth_motion("Intel(R) UHD Graphics") is False


def test_download_vendor_file_enforces_https_allowlist_and_sha256(tmp_path: Path):
    payload = b"nvidia-signed-driver-mock-bytes"
    good_sha = hashlib.sha256(payload).hexdigest()
    FakeResp = _fake_resp_cls()

    with pytest.raises(ValueError, match="Blocked"):
        download_vendor_file(
            "https://evil.example.com/driver.exe",
            dest_dir=tmp_path,
            urlopen_fn=lambda req, timeout=45: FakeResp(payload),
        )

    with pytest.raises(ValueError, match="Blocked"):
        download_vendor_file(
            "http://cn.download.nvidia.com/Windows/572.84/driver.exe",
            dest_dir=tmp_path,
            urlopen_fn=lambda req, timeout=45: FakeResp(payload),
        )

    out = download_vendor_file(
        "https://cn.download.nvidia.com/Windows/621.40/621.40-notebook.exe",
        sha256=good_sha,
        dest_dir=tmp_path,
        urlopen_fn=lambda req, timeout=45: FakeResp(payload),
    )
    assert out["ok"] is True
    assert (tmp_path / "621.40-notebook.exe").read_bytes() == payload


def test_framegen_reads_settings_xml_creates_profile_binds_and_dedupes_autoscale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    repo_root = Path(__file__).resolve().parents[1]
    shutil.copytree(repo_root / "registry" / "plugins" / "framegen-sharpen", paths.plugins_dir / "framegen-sharpen")

    ls_xml = tmp_path / "LS" / "Settings.xml"
    ls_xml.parent.mkdir(parents=True, exist_ok=True)
    ls_xml.write_text(SAMPLE_LS_XML, encoding="utf-8")
    monkeypatch.setenv("WIREDECK_LS_SETTINGS_XML", str(ls_xml))

    fake_ls = tmp_path / "Steam" / "LosslessScaling.exe"
    fake_ls.parent.mkdir(parents=True, exist_ok=True)
    fake_ls.write_bytes(b"MZ")
    fake_client = tmp_path / "XStreaming" / "XStreaming.exe"
    fake_client.parent.mkdir(parents=True, exist_ok=True)
    fake_client.write_bytes(b"MZ")

    sys_env = {
        "max_refresh_hz": 240,
        "is_dual_gpu": True,
        "on_battery": False,
        "primary_nvidia_gpu": {"name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU", "driver_version": "572.84", "supports_smooth_motion": True},
    }

    # 1. Dynamic profile selector lists existing XML profiles + parsed Ctrl+Alt+S hotkey
    dyn = resolve_plugin_dynamic_options(paths, "framegen-sharpen", {"client_exe_path": str(fake_client), "system_env": sys_env})
    assert dyn["ok"] is True
    vals = [o["value"] for o in dyn["options_by_key"]["ls_profile"]]
    assert vals == ["__auto__", "默认", "XStreamingAuto"]
    assert dyn["meta_by_key"]["ls_profile"]["detected_hotkey"] == "Ctrl+Alt+S"

    # 2. Action create_streaming_profile backs up XML once and writes WireDeck 串流高刷推荐 (240 Hz, WGC, LS1, LSFG3)
    save_plugin_config(paths, "framegen-sharpen", enabled=True, settings={"engine": "lossless_scaling", "exe_path": str(fake_ls), "delay_sec": 0.5})
    act_res = run_plugin_action(
        paths,
        "framegen-sharpen",
        "create_streaming_profile",
        context={"client_exe_path": str(fake_client), "system_env": sys_env, "data_dir": str(paths.data_dir)},
    )
    assert act_res["ok"] is True and act_res["code"] == "STREAM_PROFILE_CREATED"
    assert (paths.runtime_dir / "ls_settings_backup.xml").is_file()

    parsed_xml = ET.fromstring(ls_xml.read_text(encoding="utf-8"))
    rec_prof = next(p for p in parsed_xml.findall(".//GameProfiles/Profile") if p.findtext("Title") == "WireDeck 串流高刷推荐")
    assert rec_prof.findtext("FrameGeneration") == "LSFG3"
    assert rec_prof.findtext("ScalingType") == "LS1"
    assert rec_prof.findtext("CaptureApi") == "WGC"
    assert rec_prof.findtext("LSFG3Target") == "240"

    # 3. Clear Path on 默认, bind via before_client_launch, then after_launch reads Ctrl+Alt+S from XML (since AutoScale=false)
    save_plugin_config(
        paths,
        "framegen-sharpen",
        enabled=True,
        settings={
            "engine": "lossless_scaling",
            "ls_profile": "默认",
            "exe_path": str(fake_ls),
            "trigger_mode": "smart",
            "auto_launch_engine": True,
            "auto_trigger_scale": True,
            "scale_hotkey": "",
            "delay_sec": 0.3,
            "keep_awake": True,
            "enabled_presets": ["xbox"],
        },
    )
    ops_events: list[tuple[Any, ...]] = []
    fake_ops = Win32Ops(
        is_process_running=lambda n: False,
        launch_process=lambda exe, args=None, cwd="", minimized=False: ops_events.append(("launch", exe, minimized)) or 111,
        restart_process=lambda exe, args=None, cwd="", minimized=False: ops_events.append(("restart", exe, minimized)) or 112,
        wait_window=lambda **kw: ops_events.append(("wait", kw.get("pid"))) or 222,
        sleep=lambda sec: ops_events.append(("sleep", sec)),
        focus_window=lambda hwnd: ops_events.append(("focus", hwnd)) or True,
        send_hotkey=lambda keys: ops_events.append(("hotkey", keys)) or True,
        set_keep_awake=lambda en: ops_events.append(("awake", en)) or True,
    )
    ctx_xbox = {
        "preset": "xbox",
        "client_pid": 9001,
        "client_exe": "XStreaming.exe",
        "client_exe_path": str(fake_client),
        "system_env": sys_env,
        "data_dir": str(paths.data_dir),
    }
    r_before = emit_plugin_hook(paths, "before_client_launch", ctx_xbox, win32_ops=fake_ops)
    r_after = emit_plugin_hook(paths, "after_client_launch", ctx_xbox, win32_ops=fake_ops)
    assert r_before[0].code == "ENHANCER_PROFILE_BOUND"
    assert r_after[0].code == "SCALE_TRIGGER_DISPATCHED"
    assert ("awake", True) in ops_events
    assert ("hotkey", "Ctrl+Alt+S") in ops_events

    rebound_xml = ET.fromstring(ls_xml.read_text(encoding="utf-8"))
    def_prof = next(p for p in rebound_xml.findall(".//GameProfiles/Profile") if p.findtext("Title") == "默认")
    assert def_prof.findtext("Path") == str(fake_client)

    # 4. Smart AutoScale deduplication: when XStreamingAuto (AutoScale=true) matches Path, NO duplicate hotkey is sent!
    save_plugin_config(paths, "framegen-sharpen", settings={"ls_profile": "XStreamingAuto", "trigger_mode": "smart"})
    ops_events.clear()
    r_auto = emit_plugin_hook(
        paths,
        "after_client_launch",
        {**ctx_xbox, "client_exe_path": r"C:\Games\XStreaming\XStreaming.exe"},
        win32_ops=fake_ops,
    )
    assert r_auto[0].code == "AUTOSCALE_HANDLED"
    assert ops_events == []


def test_nvidia_toolkit_and_both_catalog_plugins_sha256_install(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    repo_root = Path(__file__).resolve().parents[1]
    index_path = repo_root / "registry" / "index.json"
    index_bytes = index_path.read_bytes()

    url_map: dict[str, bytes] = {
        "https://raw.githubusercontent.com/Iraryi/WireDeck/main/registry/index.json": index_bytes,
    }
    for pid in ("framegen-sharpen", "nvidia-driver-toolkit"):
        pdir = repo_root / "registry" / "plugins" / pid
        for fpath in pdir.rglob("*"):
            if fpath.is_file():
                rel = fpath.relative_to(pdir).as_posix()
                url_map[f"https://raw.githubusercontent.com/Iraryi/WireDeck/main/registry/plugins/{pid}/{rel}"] = fpath.read_bytes()

    FakeResp = _fake_resp_cls()

    def fake_urlopen(req, timeout=12.0):
        return FakeResp(url_map[req.full_url])

    cat = fetch_plugin_catalog(urlopen_fn=fake_urlopen)
    assert [p["id"] for p in cat["plugins"]] == ["framegen-sharpen", "nvidia-driver-toolkit"]

    for pid in ("framegen-sharpen", "nvidia-driver-toolkit"):
        res = install_plugin_from_github(paths, plugin_id=pid, urlopen_fn=fake_urlopen)
        assert res.ok is True, res.message
        m, errs = validate_plugin_dir(paths.plugins_dir / pid)
        assert m is not None and not errs

    sys_env = {
        "gpus": [{"name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU", "driver_version": "572.84", "is_nvidia": True, "supports_smooth_motion": True}],
        "primary_nvidia_gpu": {"name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU", "driver_version": "572.84", "supports_smooth_motion": True},
        "is_dual_gpu": True,
        "max_refresh_hz": 240,
    }
    chk = run_plugin_check(paths, "nvidia-driver-toolkit", {"system_env": sys_env})
    assert chk["ok"] is True and "572.84" in chk["message"] and "Smooth Motion" in chk["message"]

    fake_client = tmp_path / "XStreaming.exe"
    fake_client.write_bytes(b"MZ")
    gpu_calls: list[tuple[str, str]] = []
    fake_ops = Win32Ops(
        set_gpu_preference=lambda exe, mode, backup_file=None: gpu_calls.append((exe, mode)) or True,
    )
    res_bind = run_plugin_action(
        paths,
        "nvidia-driver-toolkit",
        "bind_dgpu",
        context={"client_exe_path": str(fake_client), "system_env": sys_env, "data_dir": str(paths.data_dir)},
        win32_ops=fake_ops,
    )
    assert res_bind["ok"] is True
    assert (str(fake_client), "high_performance") in gpu_calls


def test_quick_play_no_plugins_and_plugin_opt_override(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    parsed_opts = _parse_plugin_opt_args(["demo.enabled=false", "demo.delay_sec=3.5", "demo.tag=test"])
    assert parsed_opts == {"demo": {"enabled": False, "delay_sec": 3.5, "tag": "test"}}

    pdir = paths.plugins_dir / "override-check"
    (pdir / "hooks").mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "schema": "wiredeck-plugin/v2",
                "id": "override-check",
                "name": "Override Check",
                "version": "1.0.0",
                "hooks": {"before_client_launch": "hooks/bcl.ps1"},
                "settings_schema": [{"key": "tag", "type": "text", "label": "Tag", "default": "orig"}],
            }
        ),
        encoding="utf-8",
    )
    out_log = tmp_path / "tag.txt"
    q_log = str(out_log).replace("'", "''")
    (pdir / "hooks" / "bcl.ps1").write_bytes(
        b"\xef\xbb\xbf"
        + (
            f"$ctx = Get-Content -LiteralPath $env:WIREDECK_HOOK_CONTEXT_FILE -Raw -Encoding UTF8 | ConvertFrom-Json\n"
            f"Add-Content -LiteralPath '{q_log}' -Value ([string]$ctx.plugin_settings.tag) -Encoding UTF8\n"
            f"Write-Output '{{\"ok\":true}}'\n"
        ).encode("utf-8")
    )

    ready_snap: dict[str, Any] = {
        "os_version": "Windows 11",
        "is_admin": False,
        "net_adapters": [{"name": "Ethernet", "interface_description": "2.5GbE", "interface_index": 12, "status": "Up", "link_speed_bps": 1_000_000_000, "is_virtual": False, "is_wireless": False, "powersave_enabled": []}],
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
    }
    api = WireDeckApi(paths=paths, snapshot_fn=lambda *a, **kw: ready_snap)

    # 1. With skip_plugins=True -> never runs plugin script
    ok1, _, _, _ = _run_quick_play_detailed(
        api,
        preset_id="xbox",
        launch_fn=lambda preset_id="xbox": ActionResult(ok=True, code="LAUNCHED", message="OK", details={"pid": 1}),
        skip_plugins=True,
    )
    assert ok1 is True and not out_log.exists()

    # 2. With plugin_overrides -> uses temporary value without mutating stored config
    ok2, _, _, _ = _run_quick_play_detailed(
        api,
        preset_id="xbox",
        launch_fn=lambda preset_id="xbox": ActionResult(ok=True, code="LAUNCHED", message="OK", details={"pid": 1}),
        plugin_overrides={"override-check": {"tag": "from-cli-override"}},
    )
    assert ok2 is True
    assert out_log.read_text(encoding="utf-8-sig").strip() == "from-cli-override"


def test_system_health_doctor_auto_restore_on_exit_and_plugin_split_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from wiredeck.cli import main as cli_main
    from wiredeck.config import AppConfig, RecorderSettings, load_config, save_config
    from wiredeck.system_env import evaluate_system_health
    import wiredeck.app as app_mod
    import wiredeck.cli as cli_mod

    # 1. evaluate_system_health flags battery, offline target, OBS unsynced, fallback pending
    health = evaluate_system_health(
        system_env={"on_battery": True, "max_refresh_hz": 240, "primary_resolution": "2560x1600", "primary_nvidia_gpu": {"name": "RTX 5070 Ti", "driver_version": "572.84"}},
        cfg_dict={"recorder": {"output_dir": r"E:\XboxClips", "auto_link_quick_play": True}},
        recorder_detected={"effective_backend": "obs", "obs": {"installed": True, "record_path": r"C:\Users\Demo\Videos"}},
        target_status={"online": True, "free_gb": 100.0, "warnings": []},
        effective_split={"mode": "both"},
        fallback_pending_count=2,
        installed_plugins=[{"valid": True, "enabled": True}],
    )
    codes = {w["code"] for w in health["warnings"]}
    assert health["overall"] == "warn"
    assert {"ON_BATTERY", "OBS_PATH_UNSYNCED", "FALLBACK_PENDING"} <= codes

    # 2. auto_restore_on_exit config roundtrip & silent session execution
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = AppConfig(auto_restore_on_exit=True, keep_awake_during_stream=False)
    save_config(paths, cfg)
    assert load_config(paths).auto_restore_on_exit is True

    fake_ops = Win32Ops(wait_for_pid_exit=lambda pid, poll_interval=1.0: True, set_keep_awake=lambda en: True, show_notification=lambda t, m: True)
    api = WireDeckApi(paths=paths, win32_ops=fake_ops, snapshot_fn=lambda *a, **kw: {})
    restored: list[bool] = []
    monkeypatch.setattr(app_mod, "_run_quick_restore", lambda *a, **kw: restored.append(True) or (True, ""))
    api._start_session_supervisor(4321, {"client_name": "XStreaming"}, skip_plugins=True, blocking=True)
    assert restored == [True]

    # 3. Plugin recorder_callback supports split_now
    cb = api._make_recorder_callback()
    res_cb = cb("split_now")
    assert res_cb["code"] == "NOT_RECORDING"

    # 4. CLI `doctor --json`
    monkeypatch.setattr(cli_mod, "resolve_app_paths", lambda *a, **kw: paths)
    monkeypatch.setattr(cli_mod, "collect_system_env", lambda: {"on_battery": False, "gpus": [], "max_refresh_hz": 60})
    monkeypatch.setattr(cli_mod, "list_storage_drives", lambda **kw: [])
    assert cli_main(["doctor", "--json"]) == 0
