"""Streaming client detection, SHA-256 verified official download, and launcher."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from wiredeck.models import ActionResult
from wiredeck.probes import detect_local_xstreaming_path
from wiredeck.ps_runner import CREATE_NO_WINDOW, run_powershell

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
    """Detect local XStreamingDesktop installation and Microsoft Xbox App."""
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


def launch_stream_client(prefer: str = "auto") -> ActionResult:
    """Launch installed XStreamingDesktop or Microsoft Xbox App."""
    clients = detect_stream_clients()
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
