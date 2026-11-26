"""Streaming client detection, SHA-256 verified official download, and launcher."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from wiredeck.config import AppConfig
from wiredeck.models import ActionResult
from wiredeck.paths import AppPaths, get_assets_dir
from wiredeck.presets import get_preset, normalize_preset_id
from wiredeck.probes import (
    detect_local_handheld_clients,
    detect_local_ps_clients,
    detect_local_xstreaming_path,
)
from wiredeck.ps_runner import CREATE_NO_WINDOW, PsResult, run_powershell

XSTREAMING_API_URL = "https://api.github.com/repos/Geocld/XStreamingDesktop/releases/latest"
XSTREAMING_RELEASES_URL = "https://github.com/Geocld/XStreamingDesktop/releases/latest"


@dataclass
class ReleaseAssetInfo:
    ok: bool
    version: str = ""
    asset_name: str = ""
    download_url: str = ""
    sha256: str = ""
    size_bytes: int = 0
    release_page_url: str = XSTREAMING_RELEASES_URL
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_stream_clients() -> dict[str, Any]:
    """Detect local streaming clients across Xbox, PlayStation, and Handheld presets."""
    xstreaming_path = detect_local_xstreaming_path()
    xbox_installed = False
    res = run_powershell(
        "(Get-AppxPackage -Name 'Microsoft.GamingApp' -ErrorAction SilentlyContinue) -ne $null",
        timeout=8,
    )
    if res.returncode == 0 and "true" in res.stdout.lower():
        xbox_installed = True
    return {
        "xstreaming_path": xstreaming_path,
        "xbox_app_installed": xbox_installed,
        **detect_local_ps_clients(),
        **detect_local_handheld_clients(),
    }


def fetch_latest_xstreaming_release(
    *,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> ReleaseAssetInfo:
    """Fetch latest official XStreamingDesktop Windows installer metadata & SHA-256."""
    req = urllib.request.Request(
        XSTREAMING_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "WireDeck/0.1.0",
        },
    )
    try:
        with urlopen_fn(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return ReleaseAssetInfo(
            ok=False,
            release_page_url=XSTREAMING_RELEASES_URL,
            error=f"GitHub 接口暂时异常（HTTP {exc.code}），可稍后重试或手动打开发布页下载",
        )
    except Exception as exc:
        return ReleaseAssetInfo(
            ok=False,
            release_page_url=XSTREAMING_RELEASES_URL,
            error=f"无法访问 GitHub 发布页（{exc}），请检查网络或手动下载",
        )

    tag = str(data.get("tag_name") or "")
    assets = list(data.get("assets") or [])
    chosen = None
    for a in assets:
        name = str(a.get("name") or "")
        if name.lower().endswith(".exe") and ("setup" in name.lower() or "xstreaming" in name.lower()):
            chosen = a
            break
    if chosen is None:
        for a in assets:
            if str(a.get("name") or "").lower().endswith(".exe"):
                chosen = a
                break

    if chosen is None:
        return ReleaseAssetInfo(
            ok=False,
            version=tag,
            release_page_url=str(data.get("html_url") or XSTREAMING_RELEASES_URL),
            error="未在最新官方发布中找到 Windows .exe 安装包",
        )

    digest_raw = str(chosen.get("digest") or "")
    sha256 = digest_raw.split(":", 1)[1].strip().lower() if digest_raw.lower().startswith("sha256:") else digest_raw.strip().lower()

    return ReleaseAssetInfo(
        ok=True,
        version=tag,
        asset_name=str(chosen.get("name") or ""),
        download_url=str(chosen.get("browser_download_url") or ""),
        sha256=sha256,
        size_bytes=int(chosen.get("size") or 0),
        release_page_url=str(data.get("html_url") or XSTREAMING_RELEASES_URL),
    )


def download_and_verify_asset(
    url: str,
    dest: Path,
    expected_sha256: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 30.0,
) -> Path:
    """Download official asset to dest and strictly verify SHA-256 before committing."""
    expected = expected_sha256.strip().lower()
    if expected.startswith("sha256:"):
        expected = expected.split(":", 1)[1].strip()
    if len(expected) != 64:
        raise ValueError("Missing or invalid official SHA-256 digest; refusing unverified download")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest.with_suffix(dest.suffix + ".part")

    req = urllib.request.Request(url, headers={"User-Agent": "WireDeck/0.1.0"})
    hasher = hashlib.sha256()
    downloaded = 0

    try:
        with urlopen_fn(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0) if getattr(resp, "headers", None) else 0
            with part_path.open("wb") as fh:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb is not None:
                        progress_cb(downloaded, total)

        actual = hasher.hexdigest().lower()
        if actual != expected:
            part_path.unlink(missing_ok=True)
            dest.unlink(missing_ok=True)
            raise ValueError(f"SHA-256 mismatch: expected {expected}, got {actual}")

        os.replace(part_path, dest)
        return dest
    except Exception:
        part_path.unlink(missing_ok=True)
        if not isinstance(dest, Path) or (dest.exists() and hash_file_sha256(dest) != expected):
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def hash_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def launch_stream_client(prefer: str = "auto", preset_id: str = "xbox", custom_path: str = "") -> ActionResult:
    """Launch installed streaming client matching the active device preset (custom_path takes priority)."""
    custom = custom_path.strip()
    if custom and Path(custom).is_file():
        try:
            subprocess.Popen([custom], cwd=str(Path(custom).parent))
            return ActionResult(
                ok=True,
                code="LAUNCHED",
                message=f"已启动 {Path(custom).stem}",
                details={"path": custom, "custom": True},
            )
        except Exception as exc:
            return ActionResult(
                ok=False,
                code="LAUNCH_FAILED",
                message=f"启动 {Path(custom).name} 失败: {exc}",
                exit_code=1,
            )

    pid = normalize_preset_id(preset_id)
    clients = detect_stream_clients()

    if pid == "playstation":
        for key, label in (("chiaki_path", "Chiaki-ng"), ("ps_remote_path", "PS Remote Play")):
            path = clients.get(key)
            if path:
                try:
                    subprocess.Popen([path], cwd=str(Path(path).parent))
                    return ActionResult(ok=True, code="LAUNCHED", message=f"已启动 {label}", details={"path": path})
                except Exception as exc:
                    return ActionResult(ok=False, code="LAUNCH_FAILED", message=f"启动 {label} 失败: {exc}", exit_code=1)
        return ActionResult(
            ok=False,
            code="PS_CLIENT_MISSING",
            message="未检测到 Chiaki-ng 或 PS Remote Play，请先在向导中下载",
            exit_code=1,
        )

    if pid == "handheld":
        for key, label in (("moonlight_path", "Moonlight"), ("steam_path", "Steam"), ("sunshine_path", "Sunshine")):
            path = clients.get(key)
            if path:
                try:
                    subprocess.Popen([path], cwd=str(Path(path).parent))
                    return ActionResult(ok=True, code="LAUNCHED", message=f"已启动 {label}", details={"path": path})
                except Exception as exc:
                    return ActionResult(ok=False, code="LAUNCH_FAILED", message=f"启动 {label} 失败: {exc}", exit_code=1)
        return ActionResult(
            ok=False,
            code="HANDHELD_CLIENT_MISSING",
            message="未检测到 Moonlight / Steam / Sunshine，请先在向导中安装",
            exit_code=1,
        )

    if pid in ("switch", "other"):
        return ActionResult(
            ok=True,
            code="NO_CLIENT_NEEDED",
            message="当前预设无需额外串流客户端，开启直连即可直接联机或共享网络",
            exit_code=0,
        )

    # Default: Xbox preset
    xs = clients.get("xstreaming_path")
    xbox_installed = bool(clients.get("xbox_app_installed", False))

    if prefer in ("auto", "xstreaming") and xs:
        try:
            subprocess.Popen([xs], cwd=str(Path(xs).parent))
            return ActionResult(ok=True, code="LAUNCHED", message="已启动 XStreaming", details={"path": xs})
        except Exception as exc:
            return ActionResult(ok=False, code="LAUNCH_FAILED", message=f"启动 XStreaming 失败: {exc}", exit_code=1)

    if prefer in ("auto", "xbox") and xbox_installed:
        try:
            creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.Popen(
                ["explorer.exe", r"shell:AppsFolder\Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App"],
                creationflags=creationflags,
            )
            return ActionResult(ok=True, code="LAUNCHED", message="已启动微软 Xbox 应用")
        except Exception as exc:
            return ActionResult(ok=False, code="LAUNCH_FAILED", message=f"启动 Xbox 应用失败: {exc}", exit_code=1)

    return ActionResult(
        ok=False,
        code="STREAM_CLIENT_MISSING",
        message="未检测到 XStreaming 或 Xbox 应用，请先在向导中下载",
        exit_code=1,
    )


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_play_shortcut_name(cfg: AppConfig) -> str:
    """Build a safe localized Windows .lnk filename for 1-click Quick Play."""
    preset_labels = {
        "xbox": "Xbox",
        "playstation": "PlayStation",
        "switch": "Switch",
        "handheld": "Handheld",
        "other": "Other",
    }
    mode_key = "wired" if cfg.active_transport != "wireless" else ("lan" if cfg.wireless_submode == "lan" else "hotspot")
    lang = cfg.language if cfg.language in ("zh-CN", "zh-TW", "en", "ja") else "zh-CN"

    prefix_map = {
        "zh-CN": "WireDeck 一键开玩",
        "zh-TW": "WireDeck 一鍵開玩",
        "en": "WireDeck Quick Play",
        "ja": "WireDeck クイック起動",
    }
    mode_map = {
        "zh-CN": {"wired": "有线直连", "hotspot": "5G热点", "lan": "同Wi-Fi"},
        "zh-TW": {"wired": "有線直連", "hotspot": "5G熱點", "lan": "同Wi-Fi"},
        "en": {"wired": "Wired", "hotspot": "5G Hotspot", "lan": "Same Wi-Fi"},
        "ja": {"wired": "有線直結", "hotspot": "5G共有", "lan": "同一Wi-Fi"},
    }
    p_lbl = preset_labels.get(normalize_preset_id(cfg.active_preset), "Xbox")
    m_lbl = mode_map[lang][mode_key]
    return f"{prefix_map[lang]} - {p_lbl} ({m_lbl}).lnk"


def _resolve_shortcut_target(paths: AppPaths, extra_args: str) -> tuple[str, str, str, str]:
    bundled_exe = paths.exe_dir / "WireDeck.exe"
    if bundled_exe.is_file():
        target_exe = str(bundled_exe)
        arg_str = extra_args
        work_dir = str(paths.exe_dir)
    else:
        py_path = Path(sys.executable)
        pyw = py_path.with_name("pythonw.exe")
        target_exe = str(pyw if pyw.is_file() else py_path)
        arg_str = f"-m wiredeck.app {extra_args}"
        work_dir = str(paths.exe_dir)
    ico_file = get_assets_dir() / "wiredeck.ico"
    icon_loc = str(ico_file) if ico_file.is_file() else f"{target_exe},0"
    return target_exe, arg_str, work_dir, icon_loc


def _write_desktop_shortcut(
    *,
    filename: str,
    target_exe: str,
    arg_str: str,
    work_dir: str,
    icon_loc: str,
    description: str,
    language: str,
    desktop_dir: Path | None = None,
    runner: Callable[[str, int], PsResult] = run_powershell,
) -> ActionResult:
    desktop_expr = _ps_quote(str(desktop_dir)) if desktop_dir is not None else "[Environment]::GetFolderPath('Desktop')"
    ps = f"""
$ErrorActionPreference = 'Stop'
$desktop = {desktop_expr}
if (-not (Test-Path -LiteralPath $desktop)) {{ New-Item -ItemType Directory -Path $desktop -Force | Out-Null }}
$lnkPath = Join-Path $desktop {_ps_quote(filename)}
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath = {_ps_quote(target_exe)}
$sc.Arguments = {_ps_quote(arg_str)}
$sc.WorkingDirectory = {_ps_quote(work_dir)}
$sc.IconLocation = {_ps_quote(icon_loc)}
$sc.Description = {_ps_quote(description)}
$sc.Save()
Write-Output $lnkPath
"""
    res = runner(ps, 15)
    out_path = res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""
    if res.returncode == 0 and out_path:
        msg_map = {
            "zh-CN": f"已创建桌面快捷方式：{filename}",
            "zh-TW": f"已建立桌面捷徑：{filename}",
            "en": f"Desktop shortcut created: {filename}",
            "ja": f"デスクトップショートカットを作成しました：{filename}",
        }
        return ActionResult(
            ok=True,
            code="SHORTCUT_CREATED",
            message=msg_map.get(language, msg_map["zh-CN"]),
            exit_code=0,
            details={"path": out_path, "filename": filename},
        )
    return ActionResult(
        ok=False,
        code="SHORTCUT_FAILED",
        message=f"创建桌面快捷方式失败：{res.stderr.strip() or 'WScript.Shell error'}",
        exit_code=1,
    )


def create_play_shortcut(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    desktop_dir: Path | None = None,
    runner: Callable[[str, int], PsResult] = run_powershell,
) -> ActionResult:
    """Create a Desktop .lnk shortcut that enables this mode and launches the stream client."""
    mode_sub = cfg.wireless_submode if cfg.wireless_submode in ("hotspot", "lan") else "hotspot"
    mode_tr = cfg.active_transport if cfg.active_transport in ("wired", "wireless") else "wired"
    quick_args = f"--quick-play --preset {normalize_preset_id(cfg.active_preset)} --transport {mode_tr} --submode {mode_sub}"
    target_exe, arg_str, work_dir, icon_loc = _resolve_shortcut_target(paths, quick_args)
    return _write_desktop_shortcut(
        filename=build_play_shortcut_name(cfg),
        target_exe=target_exe,
        arg_str=arg_str,
        work_dir=work_dir,
        icon_loc=icon_loc,
        description="WireDeck Quick Play",
        language=cfg.language,
        desktop_dir=desktop_dir,
        runner=runner,
    )


def build_restore_shortcut_name(cfg: AppConfig) -> str:
    """Build a safe localized Windows .lnk filename for 1-click Restore Normal Net/Wi-Fi."""
    lang = cfg.language if cfg.language in ("zh-CN", "zh-TW", "en", "ja") else "zh-CN"
    names = {
        "zh-CN": "WireDeck 恢复日常上网.lnk",
        "zh-TW": "WireDeck 恢復日常上網.lnk",
        "en": "WireDeck Restore Normal Net.lnk",
        "ja": "WireDeck 通常ネットへ戻す.lnk",
    }
    return names[lang]


def create_restore_shortcut(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    desktop_dir: Path | None = None,
    runner: Callable[[str, int], PsResult] = run_powershell,
) -> ActionResult:
    """Create a Desktop .lnk shortcut that restores normal Ethernet & Wi-Fi settings."""
    target_exe, arg_str, work_dir, icon_loc = _resolve_shortcut_target(paths, "--quick-restore")
    return _write_desktop_shortcut(
        filename=build_restore_shortcut_name(cfg),
        target_exe=target_exe,
        arg_str=arg_str,
        work_dir=work_dir,
        icon_loc=icon_loc,
        description="WireDeck Restore Normal Internet",
        language=cfg.language,
        desktop_dir=desktop_dir,
        runner=runner,
    )
