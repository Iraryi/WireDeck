"""Tests for long-term update engine and interactive uninstaller (default OFF companion cleanup)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from wiredeck import cli as cli_mod
from wiredeck.config import AppConfig, load_config, save_config
from wiredeck.models import ActionResult
from wiredeck.paths import resolve_app_paths
from wiredeck.uninstaller import (
    discover_uninstall_items,
    execute_uninstall_cleanup,
    find_wiredeck_desktop_shortcuts,
    launch_self_uninstall,
    parse_uninstall_command,
    scan_associated_apps,
)
from wiredeck.updater import (
    apply_staged_app_update,
    build_update_manifest,
    check_all_updates,
    check_app_update,
    compare_semver,
    download_and_stage_app_update,
    parse_sha256sums,
    update_all_plugins,
)
from wiredeck.version import UPDATE_MANIFEST_SCHEMA


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._buf = io.BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _make_urlopen(routes: dict[str, bytes]):
    def _fake_urlopen(req: Any, timeout: float = 10.0) -> _FakeResponse:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url not in routes:
            raise OSError(f"Unmocked URL: {url}")
        return _FakeResponse(routes[url])

    return _fake_urlopen


def test_semver_ordering_and_prerelease_rules():
    assert compare_semver("0.2.1", "0.2.2") == -1
    assert compare_semver("v0.2.2", "0.2.2") == 0
    assert compare_semver("0.3.0-beta.1", "0.3.0") == -1
    assert compare_semver("0.3.0-beta.1", "0.3.0-beta.2") == -1
    assert compare_semver("0.2.2", "0.3.0-beta.1") == -1


def test_config_roundtrip_persists_update_channel_and_feed(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "local")
    cfg = AppConfig(
        update_channel="beta",
        update_feed_url="https://updates.example.com/wiredeck.json",
        auto_check_updates=False,
    )
    save_config(paths, cfg)
    loaded = load_config(paths)
    assert loaded.update_channel == "beta"
    assert loaded.update_feed_url == "https://updates.example.com/wiredeck.json"
    assert loaded.auto_check_updates is False

    bad = AppConfig(update_channel="nightly")
    assert bad.update_channel == "stable"


def test_check_app_update_github_digest_and_sha256sums_fallback(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "local")
    cfg = AppConfig()
    exe_sha = "1" * 64
    zip_sha = "2" * 64

    # 1. Direct GitHub digest
    rel_latest = {
        "tag_name": "v0.3.0",
        "html_url": "https://github.com/Iraryi/WireDeck/releases/tag/v0.3.0",
        "published_at": "2026-12-01T00:00:00Z",
        "prerelease": False,
        "body": "Major optimization",
        "assets": [
            {
                "name": "WireDeck-v0.3.0-win64-setup.exe",
                "browser_download_url": "https://dl.example/setup.exe",
                "size": 1000,
                "digest": f"sha256:{exe_sha}",
            },
            {
                "name": "WireDeck-v0.3.0-win64-portable.zip",
                "browser_download_url": "https://dl.example/portable.zip",
                "size": 2000,
            },
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": "https://dl.example/SHA256SUMS.txt",
                "size": 120,
            },
        ],
    }
    routes = {
        "https://api.github.com/repos/Iraryi/WireDeck/releases/latest": json.dumps(rel_latest).encode("utf-8"),
        "https://dl.example/SHA256SUMS.txt": f"{exe_sha}  WireDeck-v0.3.0-win64-setup.exe\n{zip_sha} *WireDeck-v0.3.0-win64-portable.zip\n".encode(
            "utf-8"
        ),
    }
    res_std = check_app_update(cfg, paths, current_version="0.2.2", urlopen_fn=_make_urlopen(routes))
    assert res_std["ok"] is True
    assert res_std["has_update"] is True
    assert res_std["latest_version"] == "0.3.0"
    assert res_std["sha256"] == exe_sha
    assert res_std["sha256_ready"] is True

    # 2. Portable falls back cleanly to SHA256SUMS.txt when digest omitted
    p_app = tmp_path / "portable_app"
    p_app.mkdir()
    (p_app / "portable.mode").write_text("", encoding="utf-8")
    p_paths = resolve_app_paths(p_app, local_appdata=tmp_path / "local")
    res_port = check_app_update(cfg, p_paths, current_version="0.2.2", urlopen_fn=_make_urlopen(routes))
    assert res_port["is_portable"] is True
    assert res_port["asset_name"] == "WireDeck-v0.3.0-win64-portable.zip"
    assert res_port["sha256"] == zip_sha
    assert res_port["sha256_ready"] is True

    # 3. Same version reports up-to-date
    res_same = check_app_update(cfg, paths, current_version="0.3.0", urlopen_fn=_make_urlopen(routes))
    assert res_same["ok"] is True
    assert res_same["has_update"] is False


def test_check_app_update_beta_channel_and_custom_manifest(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "local")
    beta_cfg = AppConfig(update_channel="beta")
    releases = [
        {"tag_name": "v0.2.2", "prerelease": False, "draft": False, "assets": []},
        {
            "tag_name": "v0.3.0-beta.1",
            "prerelease": True,
            "draft": False,
            "assets": [
                {
                    "name": "WireDeck-v0.3.0-beta.1-win64-setup.exe",
                    "browser_download_url": "https://dl.example/beta.exe",
                    "digest": f"sha256:{'b' * 64}",
                }
            ],
        },
    ]
    routes = {
        "https://api.github.com/repos/Iraryi/WireDeck/releases": json.dumps(releases).encode("utf-8"),
        "https://mirror.example/update.json": json.dumps(
            {
                "schema": UPDATE_MANIFEST_SCHEMA,
                "version": "0.4.0",
                "channel": "stable",
                "release_notes": "Custom mirror update",
                "assets": {
                    "installer": {
                        "name": "WireDeck-v0.4.0-win64-setup.exe",
                        "url": "https://mirror.example/WireDeck-v0.4.0-win64-setup.exe",
                        "sha256": "c" * 64,
                        "size": 5555,
                    }
                },
            }
        ).encode("utf-8"),
    }
    res_beta = check_app_update(beta_cfg, paths, current_version="0.2.2", urlopen_fn=_make_urlopen(routes))
    assert res_beta["ok"] is True
    assert res_beta["latest_version"] == "0.3.0-beta.1"
    assert res_beta["has_update"] is True

    custom_cfg = AppConfig(update_feed_url="https://mirror.example/update.json")
    res_custom = check_app_update(custom_cfg, paths, current_version="0.2.2", urlopen_fn=_make_urlopen(routes))
    assert res_custom["ok"] is True
    assert res_custom["latest_version"] == "0.4.0"
    assert res_custom["sha256"] == "c" * 64


def test_download_stage_and_apply_standard_and_portable(tmp_path: Path):
    # Standard installer verification & launch
    std_paths = resolve_app_paths(tmp_path / "std_app", local_appdata=tmp_path / "local")
    payload = b"MZ_FAKE_INNO_INSTALLER"
    good_sha = hashlib.sha256(payload).hexdigest()
    bad_res = download_and_stage_app_update(
        {"download_url": "https://dl.example/s.exe", "sha256": "", "asset_name": "s.exe"},
        std_paths,
        urlopen_fn=_make_urlopen({"https://dl.example/s.exe": payload}),
    )
    assert not bad_res.ok and bad_res.code == "MISSING_SHA256"

    mismatch_res = download_and_stage_app_update(
        {"download_url": "https://dl.example/s.exe", "sha256": "f" * 64, "asset_name": "s.exe"},
        std_paths,
        urlopen_fn=_make_urlopen({"https://dl.example/s.exe": payload}),
    )
    assert not mismatch_res.ok

    ok_res = download_and_stage_app_update(
        {"download_url": "https://dl.example/s.exe", "sha256": good_sha, "asset_name": "WireDeck-v0.3.0-win64-setup.exe"},
        std_paths,
        urlopen_fn=_make_urlopen({"https://dl.example/s.exe": payload}),
    )
    assert ok_res.ok
    staged = Path(ok_res.details["staged_path"])
    assert staged.is_file()

    launched: list[list[str]] = []
    ap_std = apply_staged_app_update(staged, std_paths, popen_fn=lambda cmd, **_kw: launched.append(cmd))
    assert ap_std.ok
    assert "/CLOSEAPPLICATIONS" in launched[0]
    assert "/RESTARTAPPLICATIONS" in launched[0]

    # Portable zip verification, Zip Slip safety, and data/portable.mode preservation in swap script
    p_dir = tmp_path / "port_app"
    p_dir.mkdir()
    (p_dir / "portable.mode").write_text("", encoding="utf-8")
    p_paths = resolve_app_paths(p_dir, local_appdata=tmp_path / "local")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("WireDeck/WireDeck.exe", b"NEW_EXE")
        zf.writestr("WireDeck/wiredeck-cli.exe", b"NEW_CLI")
    zip_bytes = buf.getvalue()
    zip_sha = hashlib.sha256(zip_bytes).hexdigest()

    st_zip = download_and_stage_app_update(
        {"download_url": "https://dl.example/p.zip", "sha256": zip_sha, "asset_name": "WireDeck-v0.3.0-win64-portable.zip"},
        p_paths,
        urlopen_fn=_make_urlopen({"https://dl.example/p.zip": zip_bytes}),
    )
    assert st_zip.ok
    p_cmds: list[list[str]] = []
    ap_port = apply_staged_app_update(
        Path(st_zip.details["staged_path"]),
        p_paths,
        is_portable=True,
        pid=999999,
        popen_fn=lambda cmd, **_kw: p_cmds.append(cmd),
    )
    assert ap_port.ok
    assert ap_port.details["mode"] == "portable"
    script_file = Path(ap_port.details["script_path"])
    assert script_file.is_file()
    script_txt = script_file.read_text(encoding="utf-8-sig")
    assert "'data', 'portable.mode'" in script_txt
    script_file.unlink(missing_ok=True)


def test_check_all_updates_and_update_plugins_preserves_user_config(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "local")
    cfg = AppConfig(plugin_registry_url="https://raw.example/index.json")

    # Install an older plugin v1.0.0 with user custom setting preserved in plugin_configs
    pdir = paths.plugins_dir / "demo-plg"
    pdir.mkdir(parents=True)
    check_ps1 = b"Write-Output '{\"status\":\"ok\"}'"
    c_sha = hashlib.sha256(check_ps1).hexdigest()
    (pdir / "check.ps1").write_bytes(check_ps1)
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "schema": "wiredeck-plugin/v2",
                "id": "demo-plg",
                "name": "Demo Plugin",
                "version": "1.0.0",
                "author": "Tester",
                "category": "video-enhance",
                "description": "v1",
                "diagnostic_check": "check.ps1",
                "settings_schema": [{"key": "custom_opt", "type": "text", "label": "Opt", "default": "orig"}],
            }
        ),
        encoding="utf-8",
    )
    (paths.plugin_configs_dir / "demo-plg.json").write_text(
        json.dumps({"enabled": True, "settings": {"custom_opt": "user-kept-value"}}),
        encoding="utf-8",
    )

    new_manifest = json.dumps(
        {
            "schema": "wiredeck-plugin/v2",
            "id": "demo-plg",
            "name": "Demo Plugin",
            "version": "1.2.0",
            "author": "Tester",
            "category": "video-enhance",
            "description": "v1.2",
            "diagnostic_check": "check.ps1",
            "settings_schema": [{"key": "custom_opt", "type": "text", "label": "Opt", "default": "orig"}],
        }
    ).encode("utf-8")
    m_sha = hashlib.sha256(new_manifest).hexdigest()
    catalog = {
        "schema": "wiredeck-registry/v1",
        "plugins": [
            {
                "id": "demo-plg",
                "name": "Demo Plugin",
                "version": "1.2.0",
                "files": [
                    {"path": "plugin.json", "url": "https://raw.example/demo/plugin.json", "sha256": m_sha},
                    {"path": "check.ps1", "url": "https://raw.example/demo/check.ps1", "sha256": c_sha},
                ],
            }
        ],
    }
    routes = {
        "https://api.github.com/repos/Iraryi/WireDeck/releases/latest": json.dumps(
            {"tag_name": "v0.2.2", "assets": []}
        ).encode("utf-8"),
        "https://raw.example/index.json": json.dumps(catalog).encode("utf-8"),
        "https://raw.example/demo/plugin.json": new_manifest,
        "https://raw.example/demo/check.ps1": check_ps1,
    }

    summary = check_all_updates(paths, cfg, current_version="0.2.2", urlopen_fn=_make_urlopen(routes))
    assert summary["has_any_update"] is True
    assert summary["total_updates"] == 1
    assert summary["plugins"]["updates_available"][0]["latest_version"] == "1.2.0"

    upd_res = update_all_plugins(paths, cfg, urlopen_fn=_make_urlopen(routes))
    assert upd_res.ok
    assert upd_res.details["updated"] == [{"id": "demo-plg", "version": "1.2.0"}]
    saved_cfg = json.loads((paths.plugin_configs_dir / "demo-plg.json").read_text(encoding="utf-8"))
    assert saved_cfg["settings"]["custom_opt"] == "user-kept-value"


def test_discover_uninstall_items_defaults_off_and_excludes_system_and_clips(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "local")
    clips_dir = tmp_path / "USB_Clips"
    clips_dir.mkdir()
    clip_file = clips_dir / "1_xbox_highlight.mkv"
    clip_file.write_bytes(b"VIDEO")
    cfg = AppConfig()
    cfg.recorder.output_dir = str(clips_dir)
    save_config(paths, cfg)
    (paths.plugins_dir / "x").mkdir(parents=True)
    (paths.plugins_dir / "x" / "a.txt").write_text("hi", encoding="utf-8")

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    wd_lnk = desktop / "XStreaming.lnk"
    wd_lnk.write_bytes(b"\x00" + "C:\\Program Files\\WireDeck\\WireDeck.exe".encode("utf-16le"))
    other_lnk = desktop / "Chrome.lnk"
    other_lnk.write_bytes(b"\x00" + "C:\\Program Files\\Google\\Chrome\\chrome.exe".encode("utf-16le"))

    mock_registry = [
        {
            "display_name": "XStreaming",
            "display_version": "1.9.6",
            "install_location": "C:\\Program Files\\XStreaming",
            "uninstall_string": '"C:\\Program Files\\XStreaming\\Uninstall.exe"',
        },
        {
            "display_name": "OBS Studio",
            "display_version": "31.0.0",
            "install_location": "C:\\Program Files\\obs-studio",
            "uninstall_string": '"C:\\Program Files\\obs-studio\\uninstall.exe"',
        },
        {
            "display_name": "NVIDIA Graphics Driver 566.14",
            "display_version": "566.14",
            "uninstall_string": "C:\\Windows\\SysWOW64\\RunDll32.EXE",
        },
        {
            "display_name": "Microsoft Edge WebView2 Runtime",
            "display_version": "130.0",
            "uninstall_string": "C:\\setup.exe --uninstall",
        },
        {
            "display_name": "Xbox Game Bar",
            "display_version": "7.0",
            "uninstall_string": "powershell.exe ...",
        },
        {
            "display_name": "Steam",
            "display_version": "2.10",
            "uninstall_string": "C:\\Program Files (x86)\\Steam\\uninstall.exe",
        },
        {
            "display_name": "WireDeck",
            "display_version": "0.2.2",
            "uninstall_string": "unins000.exe",
        },
    ]

    scan = discover_uninstall_items(
        paths,
        cfg,
        nat_active=False,
        wifi_tuned=False,
        registry_entries=mock_registry,
        desktop_dir=desktop,
    )
    assert scan["associated_cleanup_default_enabled"] is False
    assert scan["recorded_clips_protected"] is True

    # ALL optional WireDeck items and ALL companion apps MUST default UNCHECKED
    assert all(item["default_selected"] is False for item in scan["wiredeck_items"])
    assoc_names = [a["name"] for a in scan["associated_apps"]]
    assert assoc_names == ["XStreaming", "OBS Studio"]
    assert all(a["default_selected"] is False for a in scan["associated_apps"])

    # Empty selection touches NOTHING
    invoked: list[str] = []
    noop_res = execute_uninstall_cleanup(
        paths,
        cfg,
        [],
        discovered=scan,
        desktop_dir=desktop,
        launch_uninstall_fn=lambda e: (invoked.append(e["id"]), ActionResult(ok=True, code="OK", message="ok"))[1],
    )
    assert noop_res.ok
    assert invoked == []
    assert wd_lnk.is_file()
    assert paths.config_file.is_file()
    assert clip_file.is_file()

    # Selecting shortcuts + OBS only leaves XStreaming, Chrome.lnk, and recorded video clips 100% intact
    sel_res = execute_uninstall_cleanup(
        paths,
        cfg,
        ["wd_shortcuts", "wd_plugins", "wd_userdata", "assoc_obs"],
        discovered=scan,
        desktop_dir=desktop,
        launch_uninstall_fn=lambda e: (invoked.append(e["id"]), ActionResult(ok=True, code="OK", message=f"uninstalled {e['name']}"))[1],
    )
    assert sel_res.ok
    assert invoked == ["assoc_obs"]
    assert not wd_lnk.exists()
    assert other_lnk.is_file()
    assert not paths.plugins_dir.exists()
    assert not paths.config_file.exists()
    assert clip_file.is_file()


def test_parse_uninstall_command_and_manifest_builder(tmp_path: Path):
    kind, cmd = parse_uninstall_command('"C:\\Program Files\\OBS Studio\\uninstall.exe" /S')
    assert kind == "argv"
    assert cmd == ["C:\\Program Files\\OBS Studio\\uninstall.exe", "/S"]

    kind2, uri = parse_uninstall_command("steam://uninstall/993090")
    assert kind2 == "uri"
    assert uri == "steam://uninstall/993090"

    rel_dir = tmp_path / "rel"
    rel_dir.mkdir()
    (rel_dir / "WireDeck-v0.2.2-win64-setup.exe").write_bytes(b"SETUP")
    (rel_dir / "WireDeck-v0.2.2-win64-portable.zip").write_bytes(b"ZIP")
    manifest = build_update_manifest(
        "0.2.2",
        rel_dir,
        base_download_url="https://github.com/Iraryi/WireDeck/releases/download/v0.2.2",
    )
    assert manifest["schema"] == UPDATE_MANIFEST_SCHEMA
    assert manifest["assets"]["installer"]["sha256"] == hashlib.sha256(b"SETUP").hexdigest()
    assert manifest["assets"]["portable"]["sha256"] == hashlib.sha256(b"ZIP").hexdigest()

    assert parse_sha256sums("deadbeef" * 8 + " *WireDeck-v0.2.2-win64-setup.exe") == {
        "wiredeck-v0.2.2-win64-setup.exe": "deadbeef" * 8
    }


def test_cli_update_and_uninstall_scan_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    fake_paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "local")
    monkeypatch.setattr(cli_mod, "resolve_app_paths", lambda: fake_paths)
    monkeypatch.setattr(
        cli_mod,
        "discover_uninstall_items",
        lambda paths, cfg: {
            "ok": True,
            "wiredeck_items": [],
            "associated_apps": [{"id": "assoc_obs", "name": "OBS Studio", "default_selected": False}],
            "associated_cleanup_default_enabled": False,
        },
    )
    rc = cli_mod.main(["uninstall-scan", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["associated_cleanup_default_enabled"] is False
    assert out["associated_apps"][0]["default_selected"] is False


def test_launch_self_uninstall_uses_silent_inno_when_present(tmp_path: Path):
    app_dir = tmp_path / "WireDeck"
    app_dir.mkdir()
    unins = app_dir / "unins000.exe"
    unins.write_bytes(b"MZ")
    paths = resolve_app_paths(app_dir, local_appdata=tmp_path / "local")
    cmds: list[list[str]] = []
    res = launch_self_uninstall(paths, popen_fn=lambda cmd, **_kw: cmds.append(cmd))
    assert res.ok
    assert cmds[0][0] == str(unins)
    assert "/SILENT" in cmds[0]
