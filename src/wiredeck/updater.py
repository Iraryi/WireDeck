"""WireDeck long-term extensible update engine (core app + plugins + custom manifest)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from wiredeck.config import AppConfig
from wiredeck.models import ActionResult
from wiredeck.paths import AppPaths, ensure_within
from wiredeck.plugins import (
    fetch_plugin_catalog,
    install_plugin_from_github,
    list_installed_plugins,
)
from wiredeck.stream_client import download_and_verify_asset
from wiredeck.version import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_UPDATE_API_URL,
    DEFAULT_UPDATE_RELEASES_URL,
    UPDATE_MANIFEST_SCHEMA,
)

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SHA256_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+[*]?(.+?)\s*$")


def _parse_semver(raw: str) -> tuple[tuple[int, int, int], list[tuple[int, int | str]]]:
    cleaned = (raw or "0").strip().lstrip("vV")
    core_build = cleaned.split("+", 1)[0]
    if "-" in core_build:
        core_part, pre_part = core_build.split("-", 1)
    else:
        core_part, pre_part = core_build, ""

    nums: list[int] = []
    for seg in core_part.split("."):
        m = re.match(r"^(\d+)", seg.strip())
        nums.append(int(m.group(1)) if m else 0)
    while len(nums) < 3:
        nums.append(0)

    pre_tokens: list[tuple[int, int | str]] = []
    if pre_part.strip():
        for seg in re.split(r"[.\-]", pre_part.strip()):
            if not seg:
                continue
            if seg.isdigit():
                pre_tokens.append((0, int(seg)))
            else:
                pre_tokens.append((1, seg.lower()))
    return (nums[0], nums[1], nums[2]), pre_tokens


def compare_semver(v1: str, v2: str) -> int:
    """Compare SemVer versions cleanly (-1 if v1 < v2, 0 if equal, 1 if v1 > v2)."""
    c1, pre1 = _parse_semver(v1)
    c2, pre2 = _parse_semver(v2)
    if c1 < c2:
        return -1
    if c1 > c2:
        return 1
    if not pre1 and not pre2:
        return 0
    if not pre1 and pre2:
        return 1  # stable > prerelease
    if pre1 and not pre2:
        return -1
    for t1, t2 in zip(pre1, pre2):
        if t1 == t2:
            continue
        return -1 if t1 < t2 else 1
    if len(pre1) < len(pre2):
        return -1
    if len(pre1) > len(pre2):
        return 1
    return 0


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse standard SHA256SUMS.txt into `{lowercase_filename: sha256_hex}`."""
    mapping: dict[str, str] = {}
    for line in (text or "").splitlines():
        m = SHA256_LINE_RE.match(line.strip())
        if not m:
            continue
        digest = m.group(1).lower()
        name = Path(m.group(2).strip()).name.lower()
        if name:
            mapping[name] = digest
    return mapping


def resolve_update_feed_url(cfg: AppConfig, *, override_url: str = "", override_channel: str = "") -> str:
    """Resolve the active update API / manifest URL based on config and channel."""
    custom = (override_url or cfg.update_feed_url or "").strip()
    if custom:
        return custom
    channel = (override_channel or cfg.update_channel or "stable").strip().lower()
    if channel == "beta":
        return DEFAULT_UPDATE_RELEASES_URL
    return DEFAULT_UPDATE_API_URL


def _fetch_json_any(
    url: str,
    *,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json, application/json",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        },
    )
    with urlopen_fn(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def _fetch_text(
    url: str,
    *,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "text/plain, */*",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        },
    )
    with urlopen_fn(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8-sig", errors="replace")


def _extract_sha256_from_digest(raw_digest: Any) -> str:
    s = str(raw_digest or "").strip()
    if s.lower().startswith("sha256:"):
        s = s.split(":", 1)[1].strip()
    return s.lower() if SHA256_RE.match(s) else ""


def _pick_github_asset(assets: list[dict[str, Any]], *, is_portable: bool) -> dict[str, Any] | None:
    preferred_suffix = "-win64-portable.zip" if is_portable else "-win64-setup.exe"
    fallback_suffix = ".zip" if is_portable else ".exe"
    for a in assets:
        name = str(a.get("name", "")).strip().lower()
        if name.endswith(preferred_suffix):
            return a
    for a in assets:
        name = str(a.get("name", "")).strip().lower()
        if name.endswith(fallback_suffix) and "portable" in name if is_portable else name.endswith(fallback_suffix):
            return a
    return None


def parse_update_payload(
    payload: Any,
    *,
    is_portable: bool,
    channel: str = "stable",
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Normalize either a `wiredeck-update/v1` manifest or GitHub Releases payload."""
    if isinstance(payload, dict) and str(payload.get("schema", "")).strip() == UPDATE_MANIFEST_SCHEMA:
        assets_map = payload.get("assets") if isinstance(payload.get("assets"), dict) else {}
        key = "portable" if is_portable else "installer"
        asset = assets_map.get(key) if isinstance(assets_map.get(key), dict) else {}
        sha = _extract_sha256_from_digest(asset.get("sha256", ""))
        version = str(payload.get("version", "")).strip().lstrip("vV")
        return {
            "source_kind": UPDATE_MANIFEST_SCHEMA,
            "version": version,
            "channel": str(payload.get("channel") or channel or "stable"),
            "is_prerelease": bool(payload.get("prerelease", False)),
            "published_at": str(payload.get("published_at") or ""),
            "release_url": str(payload.get("release_url") or ""),
            "release_notes": str(payload.get("release_notes") or ""),
            "asset_name": str(asset.get("name") or Path(str(asset.get("url") or "")).name),
            "download_url": str(asset.get("url") or "").strip(),
            "sha256": sha,
            "size_bytes": int(asset.get("size") or 0),
            "format": "zip" if is_portable else "exe",
        }

    # GitHub Releases API (list or single release dict)
    release: dict[str, Any] | None = None
    if isinstance(payload, list):
        candidates: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            if channel == "stable" and item.get("prerelease"):
                continue
            tag = str(item.get("tag_name") or "").strip().lstrip("vV")
            if tag:
                candidates.append(item)
        if candidates:
            candidates.sort(
                key=lambda r: _parse_semver(str(r.get("tag_name") or "0.0.0")),
                reverse=True,
            )
            release = candidates[0]
    elif isinstance(payload, dict):
        if "tag_name" in payload:
            release = payload

    if not release:
        raise ValueError("未找到可用的发布版本条目")

    version = str(release.get("tag_name") or "").strip().lstrip("vV")
    raw_assets = [a for a in (release.get("assets") or []) if isinstance(a, dict)]
    picked = _pick_github_asset(raw_assets, is_portable=is_portable)
    asset_name = str(picked.get("name", "")).strip() if picked else ""
    download_url = str(picked.get("browser_download_url", "")).strip() if picked else ""
    size_bytes = int(picked.get("size") or 0) if picked else 0
    sha = _extract_sha256_from_digest(picked.get("digest")) if picked else ""

    if picked and not sha:
        sums_asset = next(
            (a for a in raw_assets if str(a.get("name", "")).strip().lower() == "sha256sums.txt"),
            None,
        )
        sums_url = str(sums_asset.get("browser_download_url", "")).strip() if sums_asset else ""
        if sums_url.startswith(("https://", "http://")):
            try:
                sums_txt = _fetch_text(sums_url, urlopen_fn=urlopen_fn, timeout=timeout)
                sha_map = parse_sha256sums(sums_txt)
                sha = sha_map.get(asset_name.lower(), "")
            except Exception:
                sha = ""

    return {
        "source_kind": "github-releases",
        "version": version,
        "channel": "beta" if release.get("prerelease") else (channel or "stable"),
        "is_prerelease": bool(release.get("prerelease", False)),
        "published_at": str(release.get("published_at") or ""),
        "release_url": str(release.get("html_url") or ""),
        "release_notes": str(release.get("body") or ""),
        "asset_name": asset_name,
        "download_url": download_url,
        "sha256": sha,
        "size_bytes": size_bytes,
        "format": "zip" if is_portable else "exe",
    }


def check_app_update(
    cfg: AppConfig,
    paths: AppPaths,
    *,
    current_version: str = APP_VERSION,
    override_channel: str = "",
    override_url: str = "",
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Query the configured update feed and compare with current WireDeck version."""
    channel = (override_channel or cfg.update_channel or "stable").strip().lower()
    if channel not in ("stable", "beta"):
        channel = "stable"
    feed_url = resolve_update_feed_url(cfg, override_url=override_url, override_channel=channel)
    base = {
        "ok": False,
        "has_update": False,
        "current_version": current_version,
        "latest_version": "",
        "channel": channel,
        "feed_url": feed_url,
        "is_portable": paths.is_portable,
        "asset_name": "",
        "download_url": "",
        "sha256": "",
        "sha256_ready": False,
        "size_bytes": 0,
        "release_notes": "",
        "release_url": "",
        "published_at": "",
        "error": "",
    }
    try:
        raw = _fetch_json_any(feed_url, urlopen_fn=urlopen_fn, timeout=timeout)
        parsed = parse_update_payload(
            raw,
            is_portable=paths.is_portable,
            channel=channel,
            urlopen_fn=urlopen_fn,
            timeout=timeout,
        )
        latest = str(parsed.get("version") or "").strip()
        has_update = bool(latest and compare_semver(current_version, latest) < 0)
        sha = str(parsed.get("sha256") or "").lower()
        return {
            **base,
            "ok": True,
            "has_update": has_update,
            "latest_version": latest,
            "channel": str(parsed.get("channel") or channel),
            "is_prerelease": bool(parsed.get("is_prerelease", False)),
            "asset_name": str(parsed.get("asset_name") or ""),
            "download_url": str(parsed.get("download_url") or ""),
            "sha256": sha,
            "sha256_ready": bool(SHA256_RE.match(sha)),
            "size_bytes": int(parsed.get("size_bytes") or 0),
            "release_notes": str(parsed.get("release_notes") or ""),
            "release_url": str(parsed.get("release_url") or ""),
            "published_at": str(parsed.get("published_at") or ""),
        }
    except urllib.error.HTTPError as exc:
        base["error"] = f"更新源请求失败（HTTP {exc.code}）"
        return base
    except Exception as exc:
        base["error"] = f"检查主程序更新失败：{exc}"
        return base


def check_plugins_updates(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Compare installed GitHub plugins against the remote registry catalog."""
    installed = list_installed_plugins(paths)
    cat = fetch_plugin_catalog(cfg.plugin_registry_url, urlopen_fn=urlopen_fn, timeout=timeout)
    catalog_by_id = {
        str(entry.get("id", "")).strip(): entry
        for entry in (cat.get("plugins") or [])
        if isinstance(entry, dict) and str(entry.get("id", "")).strip()
    }

    items: list[dict[str, Any]] = []
    updates_available: list[dict[str, Any]] = []
    for plg in installed:
        manifest = plg.get("manifest") or {}
        pid = str(manifest.get("id") or "").strip()
        cur_ver = str(manifest.get("version") or "0.0.0").strip()
        remote = catalog_by_id.get(pid)
        latest_ver = str(remote.get("version") or "").strip() if remote else ""
        has_upd = bool(remote and latest_ver and compare_semver(cur_ver, latest_ver) < 0)
        row = {
            "id": pid,
            "name": str(manifest.get("name") or pid),
            "enabled": bool(plg.get("enabled", True)),
            "current_version": cur_ver,
            "latest_version": latest_ver or cur_ver,
            "has_update": has_upd,
            "in_catalog": bool(remote),
            "changelog": str((remote or {}).get("changelog") or (remote or {}).get("description") or ""),
        }
        items.append(row)
        if has_upd:
            updates_available.append(row)

    return {
        "ok": bool(cat.get("ok", False)),
        "registry_url": str(cat.get("registry_url") or ""),
        "installed_count": len(items),
        "updates_count": len(updates_available),
        "up_to_date_count": len(items) - len(updates_available),
        "updates_available": updates_available,
        "plugins": items,
        "error": str(cat.get("error") or ""),
    }


def _save_update_state_cache(paths: AppPaths, payload: dict[str, Any]) -> None:
    try:
        paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        cache_path = paths.runtime_dir / "update_state.json"
        fd, tmp = tempfile.mkstemp(prefix=".upd_", suffix=".tmp", dir=str(paths.runtime_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                fh.flush()
            os.replace(tmp, cache_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        pass


def check_all_updates(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    current_version: str = APP_VERSION,
    override_channel: str = "",
    override_url: str = "",
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Unified update check across WireDeck core binary + all installed plugins."""
    app_info = check_app_update(
        cfg,
        paths,
        current_version=current_version,
        override_channel=override_channel,
        override_url=override_url,
        urlopen_fn=urlopen_fn,
        timeout=timeout,
    )
    plugins_info = check_plugins_updates(paths, cfg, urlopen_fn=urlopen_fn, timeout=timeout)
    plugin_cnt = int(plugins_info.get("updates_count") or 0)
    core_cnt = 1 if app_info.get("has_update") else 0
    total = core_cnt + plugin_cnt
    summary = {
        "ok": bool(app_info.get("ok") or plugins_info.get("ok")),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "total_updates": total,
        "has_any_update": total > 0,
        "app": app_info,
        "plugins": plugins_info,
    }
    _save_update_state_cache(paths, summary)
    return summary


def download_and_stage_app_update(
    update_info: dict[str, Any],
    paths: AppPaths,
    *,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 120.0,
) -> ActionResult:
    """Download core update package into runtime/updates and verify mandatory SHA-256."""
    dl_url = str(update_info.get("download_url") or "").strip()
    sha256 = str(update_info.get("sha256") or "").strip().lower()
    asset_name = Path(str(update_info.get("asset_name") or "")).name.strip()
    version = str(update_info.get("latest_version") or update_info.get("version") or "").strip()

    if not dl_url.startswith(("https://", "http://")):
        return ActionResult(ok=False, code="INVALID_UPDATE_URL", message="更新包下载地址无效", exit_code=1)
    if not SHA256_RE.match(sha256):
        return ActionResult(
            ok=False,
            code="MISSING_SHA256",
            message="更新清单缺少 SHA-256 校验值，已拒绝下载未签名校验的安装包",
            exit_code=1,
        )
    if not asset_name:
        ext = ".zip" if paths.is_portable else ".exe"
        asset_name = f"WireDeck-update-{version or 'latest'}{ext}"

    updates_dir = paths.runtime_dir / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    try:
        dest = ensure_within(updates_dir, updates_dir / asset_name)
    except ValueError as exc:
        return ActionResult(ok=False, code="UNSAFE_ASSET_NAME", message=str(exc), exit_code=1)

    try:
        download_and_verify_asset(dl_url, dest, sha256, urlopen_fn=urlopen_fn, timeout=timeout)
    except Exception as exc:
        return ActionResult(ok=False, code="UPDATE_DOWNLOAD_FAILED", message=f"下载或校验更新包失败：{exc}", exit_code=1)

    return ActionResult(
        ok=True,
        code="UPDATE_STAGED",
        message=f"已完成 SHA-256 校验并下载更新包：{asset_name}",
        details={
            "staged_path": str(dest),
            "asset_name": asset_name,
            "sha256": sha256,
            "version": version,
        },
    )


def _find_extracted_portable_root(stage_dir: Path) -> Path:
    if (stage_dir / "WireDeck.exe").is_file() or (stage_dir / "wiredeck-cli.exe").is_file():
        return stage_dir
    for child in stage_dir.iterdir():
        if child.is_dir() and ((child / "WireDeck.exe").is_file() or (child / "wiredeck-cli.exe").is_file()):
            return child
    return stage_dir


def _build_portable_swap_script(
    source_root: Path,
    target_dir: Path,
    *,
    stage_dir: Path,
    pid: int,
    relaunch: bool,
) -> str:
    src_ps = str(source_root).replace("'", "''")
    dst_ps = str(target_dir).replace("'", "''")
    stg_ps = str(stage_dir).replace("'", "''")
    relaunch_line = (
        f"Start-Process -FilePath (Join-Path '{dst_ps}' 'WireDeck.exe') -WorkingDirectory '{dst_ps}' | Out-Null\n"
        if relaunch
        else ""
    )
    return (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f"$pidToWait = {int(pid)}\n"
        "for ($i = 0; $i -lt 60; $i++) {\n"
        "  if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) { break }\n"
        "  Start-Sleep -Milliseconds 500\n"
        "}\n"
        f"$src = '{src_ps}'\n"
        f"$dst = '{dst_ps}'\n"
        "Get-ChildItem -LiteralPath $src -Force | ForEach-Object {\n"
        "  if ($_.Name -in @('data', 'portable.mode')) { return }\n"
        "  $target = Join-Path $dst $_.Name\n"
        "  if ($_.PSIsContainer) {\n"
        "    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }\n"
        "    Copy-Item -LiteralPath $_.FullName -Destination $dst -Recurse -Force\n"
        "  } else {\n"
        "    Copy-Item -LiteralPath $_.FullName -Destination $target -Force\n"
        "  }\n"
        "}\n"
        f"Remove-Item -LiteralPath '{stg_ps}' -Recurse -Force\n"
        f"{relaunch_line}"
        "Remove-Item -LiteralPath $PSCommandPath -Force\n"
    )


def apply_staged_app_update(
    staged_path: Path,
    paths: AppPaths,
    *,
    is_portable: bool | None = None,
    silent: bool = False,
    pid: int | None = None,
    popen_fn: Callable[..., Any] = subprocess.Popen,
) -> ActionResult:
    """Apply a verified staged update package (in-place Inno Setup or portable zip swap)."""
    updates_dir = paths.runtime_dir / "updates"
    try:
        safe_staged = ensure_within(updates_dir, staged_path)
    except ValueError as exc:
        return ActionResult(ok=False, code="INVALID_STAGED_PATH", message=str(exc), exit_code=1)

    if not safe_staged.is_file():
        return ActionResult(ok=False, code="STAGED_MISSING", message="更新包不存在，请重新下载", exit_code=1)

    effective_portable = paths.is_portable if is_portable is None else bool(is_portable)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    if not effective_portable or safe_staged.suffix.lower() == ".exe":
        cmd = [str(safe_staged), "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS", "/NORESTART"]
        if silent:
            cmd.append("/SILENT")
        try:
            popen_fn(cmd, cwd=str(safe_staged.parent))
        except Exception as exc:
            return ActionResult(ok=False, code="INSTALLER_LAUNCH_FAILED", message=f"启动更新安装器失败：{exc}", exit_code=1)
        return ActionResult(
            ok=True,
            code="UPDATE_LAUNCHED",
            message="已启动覆盖更新安装程序，WireDeck 将关闭并保留您的配置与插件",
            details={"mode": "installer", "should_exit": True, "command": cmd},
        )

    # Portable mode zip atomic stage + post-exit swap
    token = uuid.uuid4().hex[:8]
    stage_dir = updates_dir / f"portable_stage_{token}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(safe_staged, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target_member = ensure_within(stage_dir, stage_dir / info.filename)
                target_member.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src_f, open(target_member, "wb") as out_f:
                    shutil.copyfileobj(src_f, out_f)
    except Exception as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        return ActionResult(ok=False, code="ZIP_EXTRACT_FAILED", message=f"解压便携版更新包失败：{exc}", exit_code=1)

    source_root = _find_extracted_portable_root(stage_dir)
    ps_script = Path(tempfile.gettempdir()) / f"wiredeck_portable_update_{token}.ps1"
    ps_script.write_text(
        _build_portable_swap_script(
            source_root,
            paths.exe_dir,
            stage_dir=stage_dir,
            pid=int(pid if pid is not None else os.getpid()),
            relaunch=not silent,
        ),
        encoding="utf-8-sig",
        newline="\n",
    )
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_script)]
    try:
        popen_fn(cmd, creationflags=creation_flags, close_fds=True)
    except Exception as exc:
        return ActionResult(ok=False, code="PORTABLE_SWAP_FAILED", message=f"启动便携版热替换失败：{exc}", exit_code=1)

    return ActionResult(
        ok=True,
        code="UPDATE_LAUNCHED",
        message="正在应用便携版更新，将自动保留 data 目录并重启 WireDeck",
        details={
            "mode": "portable",
            "should_exit": True,
            "stage_dir": str(stage_dir),
            "script_path": str(ps_script),
            "command": cmd,
        },
    )


def update_all_plugins(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    plugin_ids: list[str] | None = None,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
) -> ActionResult:
    """Update all outdated installed plugins cleanly while preserving plugin_configs."""
    check = check_plugins_updates(paths, cfg, urlopen_fn=urlopen_fn)
    if not check.get("ok"):
        return ActionResult(
            ok=False,
            code="CATALOG_FETCH_FAILED",
            message=str(check.get("error") or "无法连接 GitHub 插件目录"),
            exit_code=1,
        )

    candidates = list(check.get("updates_available") or [])
    if plugin_ids:
        wanted = {str(x).strip() for x in plugin_ids if str(x).strip()}
        candidates = [c for c in candidates if c.get("id") in wanted]

    if not candidates:
        return ActionResult(
            ok=True,
            code="PLUGINS_UP_TO_DATE",
            message="所有已安装插件均为最新版本",
            details={"updated": [], "failed": []},
        )

    updated: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for item in candidates:
        pid = str(item.get("id") or "")
        res = install_plugin_from_github(
            paths,
            plugin_id=pid,
            registry_url=cfg.plugin_registry_url,
            urlopen_fn=urlopen_fn,
        )
        if res.ok:
            updated.append({"id": pid, "version": str(item.get("latest_version") or "")})
        else:
            failed.append({"id": pid, "error": res.message})

    ok = len(failed) == 0
    return ActionResult(
        ok=ok,
        code="PLUGINS_UPDATED" if ok else "PLUGINS_PARTIAL_UPDATE",
        message=f"已更新 {len(updated)} 个插件" + (f"，{len(failed)} 个失败" if failed else ""),
        details={"updated": updated, "failed": failed},
        exit_code=0 if ok else 1,
    )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def build_update_manifest(
    version: str,
    release_dir: Path,
    *,
    base_download_url: str,
    channel: str = "stable",
    release_notes: str = "",
    release_url: str = "",
) -> dict[str, Any]:
    """Generate a signed/hashed `wiredeck-update/v1` JSON manifest from built release artifacts."""
    ver = version.strip().lstrip("vV")
    base = base_download_url.rstrip("/")
    setup_path = release_dir / f"WireDeck-v{ver}-win64-setup.exe"
    portable_path = release_dir / f"WireDeck-v{ver}-win64-portable.zip"

    assets: dict[str, dict[str, Any]] = {}
    if setup_path.is_file():
        assets["installer"] = {
            "name": setup_path.name,
            "url": f"{base}/{setup_path.name}",
            "sha256": _sha256_file(setup_path),
            "size": setup_path.stat().st_size,
        }
    if portable_path.is_file():
        assets["portable"] = {
            "name": portable_path.name,
            "url": f"{base}/{portable_path.name}",
            "sha256": _sha256_file(portable_path),
            "size": portable_path.stat().st_size,
        }

    return {
        "schema": UPDATE_MANIFEST_SCHEMA,
        "version": ver,
        "channel": channel,
        "prerelease": "-" in ver,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "release_url": release_url,
        "release_notes": release_notes,
        "assets": assets,
    }
