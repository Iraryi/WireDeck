"""Tier B (built EXE), Tier C (Inno Setup installs), and Tier D (real unins000.exe + sentinel) E2E tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import zipfile
from pathlib import Path

import pefile
import pytest

from wiredeck.version import APP_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = REPO_ROOT / "dist" / "WireDeck"
GUI_EXE = DIST_DIR / "WireDeck.exe"
CLI_EXE = DIST_DIR / "wiredeck-cli.exe"
RELEASE_DIR = REPO_ROOT / "release"
SETUP_EXE = RELEASE_DIR / f"WireDeck-v{APP_VERSION}-win64-setup.exe"
PORTABLE_ZIP = RELEASE_DIR / f"WireDeck-v{APP_VERSION}-win64-portable.zip"
SHA_FILE = RELEASE_DIR / "SHA256SUMS.txt"

CREATE_NO_WINDOW = 0x08000000


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _wait_until_gone(path: Path, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not path.exists():
            return True
        time.sleep(0.3)
    return not path.exists()


@pytest.mark.skipif(not CLI_EXE.is_file(), reason="Built dist/WireDeck not present yet")
def test_tier_b_dual_pe_subsystems_and_cli_contract(tmp_path: Path):
    # Verify WINDOWS_GUI (2) vs WINDOWS_CUI (3) sharing _internal
    assert (DIST_DIR / "_internal").is_dir()
    pe_gui = pefile.PE(str(GUI_EXE), fast_load=True)
    pe_cli = pefile.PE(str(CLI_EXE), fast_load=True)
    try:
        assert pe_gui.OPTIONAL_HEADER.Subsystem == 2  # IMAGE_SUBSYSTEM_WINDOWS_GUI
        assert pe_cli.OPTIONAL_HEADER.Subsystem == 3  # IMAGE_SUBSYSTEM_WINDOWS_CUI
    finally:
        pe_gui.close()
        pe_cli.close()

    # 1. diagnose --json
    r_diag = subprocess.run(
        [str(CLI_EXE), "diagnose", "--json", "--no-network", "--no-extensions"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
        creationflags=CREATE_NO_WINDOW,
    )
    assert r_diag.returncode == 0, r_diag.stderr
    diag = json.loads(r_diag.stdout)
    assert diag["schema_version"] == "1.0"
    assert diag["app_version"] == APP_VERSION
    assert len(diag["layers"]) == 6

    # 2. status --json
    r_st = subprocess.run(
        [str(CLI_EXE), "status", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        creationflags=CREATE_NO_WINDOW,
    )
    assert r_st.returncode == 0, r_st.stderr
    st = json.loads(r_st.stdout)
    assert "mode" in st and "nat_active" in st

    # 3. Non-elevated apply without --elevate exits 3 immediately with ADMIN_REQUIRED
    r_apply = subprocess.run(
        [str(CLI_EXE), "apply", "enable-direct", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        creationflags=CREATE_NO_WINDOW,
    )
    assert r_apply.returncode == 3
    app_data = json.loads(r_apply.stdout)
    assert app_data["ok"] is False
    assert app_data["code"] == "ADMIN_REQUIRED"

    # 4. export-bundle into non-ASCII + space path
    out_bundle = tmp_path / "导出 目录" / "bundle.json"
    r_exp = subprocess.run(
        [str(CLI_EXE), "export-bundle", "--out", str(out_bundle), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
        creationflags=CREATE_NO_WINDOW,
    )
    assert r_exp.returncode == 0, r_exp.stderr
    assert out_bundle.is_file()
    assert json.loads(out_bundle.read_text(encoding="utf-8"))["schema_version"] == "1.0"

    # 5. Bundled AI toolkits list & run from frozen CLI
    r_tk_list = subprocess.run(
        [str(CLI_EXE), "toolkit", "list", "--preset", "playstation", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        creationflags=CREATE_NO_WINDOW,
    )
    assert r_tk_list.returncode == 0, r_tk_list.stderr
    ps_tools = json.loads(r_tk_list.stdout)
    assert ps_tools["active_preset"] == "playstation"
    ps_tool_ids = {t["id"] for t in ps_tools["toolkits"]}
    assert "common/probe-route-table" in ps_tool_ids
    assert "playstation/check-psn-endpoints" in ps_tool_ids
    assert "xbox/audit-uwp-loopback" not in ps_tool_ids

    r_tk_run = subprocess.run(
        [str(CLI_EXE), "toolkit", "run", "other/emit-peer-config-snippets", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        creationflags=CREATE_NO_WINDOW,
    )
    assert r_tk_run.returncode == 0, r_tk_run.stderr
    tk_run = json.loads(r_tk_run.stdout)
    assert tk_run["ok"] is True
    assert "linux" in tk_run["data"] and "macos" in tk_run["data"]


@pytest.mark.skipif(not SHA_FILE.is_file(), reason="Release artifacts not built yet")
def test_tier_b_release_checksums_and_portable_zip_contents():
    assert SETUP_EXE.is_file()
    assert PORTABLE_ZIP.is_file()
    lines = [ln.strip() for ln in SHA_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    parsed = {ln.split()[1]: ln.split()[0] for ln in lines}
    assert parsed[SETUP_EXE.name] == _sha256(SETUP_EXE)
    assert parsed[PORTABLE_ZIP.name] == _sha256(PORTABLE_ZIP)

    with zipfile.ZipFile(PORTABLE_ZIP, "r") as zf:
        names = {n.replace("\\", "/") for n in zf.namelist()}
        assert "WireDeck/WireDeck.exe" in names
        assert "WireDeck/wiredeck-cli.exe" in names
        assert "WireDeck/portable.mode" in names
        assert "WireDeck/_internal/wiredeck/toolkits/manifest.json" in names


@pytest.mark.skipif(not SETUP_EXE.is_file(), reason="Setup EXE not built yet")
def test_tier_c_and_d_standard_chinese_space_path_install_and_sentinel_uninstall(tmp_path: Path):
    install_dir = tmp_path / "测试 安装 目录" / "WireDeck"
    proc = subprocess.run(
        [
            str(SETUP_EXE),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            f"/DIR={install_dir}",
            "/LANG=zh_CN",
            "/TASKS=",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        creationflags=CREATE_NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stderr

    inst_gui = install_dir / "WireDeck.exe"
    inst_cli = install_dir / "wiredeck-cli.exe"
    unins = install_dir / "unins000.exe"
    assert inst_gui.is_file()
    assert inst_cli.is_file()
    assert unins.is_file()
    # Standard mode MUST NOT ship portable.mode
    assert not (install_dir / "portable.mode").exists()

    # Run installed CLI from non-ASCII + space path
    cli_res = subprocess.run(
        [str(inst_cli), "status", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        creationflags=CREATE_NO_WINDOW,
    )
    assert cli_res.returncode == 0, cli_res.stderr
    assert json.loads(cli_res.stdout)["app_version"] == APP_VERSION

    # Tier D: Place user sentinel file inside install dir, run real unins000.exe
    sentinel = install_dir / "user_sentinel_keep.txt"
    sentinel.write_text("DO_NOT_DELETE_ME", encoding="utf-8")

    un_proc = subprocess.run(
        [str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=CREATE_NO_WINDOW,
    )
    assert un_proc.returncode == 0, un_proc.stderr
    assert _wait_until_gone(inst_gui), "WireDeck.exe was not removed by unins000.exe"
    assert not inst_cli.exists()
    assert sentinel.is_file() and sentinel.read_text(encoding="utf-8") == "DO_NOT_DELETE_ME"


@pytest.mark.skipif(not SETUP_EXE.is_file(), reason="Setup EXE not built yet")
def test_tier_c_and_d_portable_english_install_and_sentinel_uninstall(tmp_path: Path):
    install_dir = tmp_path / "Portable WireDeck"
    proc = subprocess.run(
        [
            str(SETUP_EXE),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            f"/DIR={install_dir}",
            "/LANG=english",
            "/TASKS=portablemode",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        creationflags=CREATE_NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stderr

    inst_cli = install_dir / "wiredeck-cli.exe"
    unins = install_dir / "unins000.exe"
    assert (install_dir / "portable.mode").is_file()
    assert inst_cli.is_file()
    assert unins.is_file()

    # Running CLI in portable mode writes data inside <install_dir>\data\
    exp_res = subprocess.run(
        [str(inst_cli), "export-bundle", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
        creationflags=CREATE_NO_WINDOW,
    )
    assert exp_res.returncode == 0, exp_res.stderr
    exp_json = json.loads(exp_res.stdout)
    expected_bundle = (install_dir / "data" / "exports" / "diagnostic_latest.json").resolve()
    assert Path(exp_json["bundle_path"]).resolve() == expected_bundle
    assert expected_bundle.is_file()

    # Place user extension sentinel under portable data/extensions
    ext_sentinel = install_dir / "data" / "extensions" / "custom_dsh_sentinel.txt"
    ext_sentinel.parent.mkdir(parents=True, exist_ok=True)
    ext_sentinel.write_text("KEEP_USER_EXT", encoding="utf-8")

    un_proc = subprocess.run(
        [str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=CREATE_NO_WINDOW,
    )
    assert un_proc.returncode == 0, un_proc.stderr
    assert _wait_until_gone(install_dir / "WireDeck.exe")
    assert not inst_cli.exists()
    assert ext_sentinel.is_file() and ext_sentinel.read_text(encoding="utf-8") == "KEEP_USER_EXT"
    assert expected_bundle.is_file()
