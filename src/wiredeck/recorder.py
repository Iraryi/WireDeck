"""Universal Recording Companion & External Storage Mini-System.

Supports OBS Studio, NVIDIA App / ShadowPlay, Windows Game Bar, built-in FFmpeg
direct recording, and custom external recorders while natively preserving each
tool's container/codec formats (`.mp4`, `.mkv`, `.mov`, `.flv`, `.ts`, etc.).
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from wiredeck.config import AppConfig, DEFAULT_FILENAME_TEMPLATE, VALID_CAPTURE_SCOPES, VALID_SPLIT_MODES
from wiredeck.models import ActionResult
from wiredeck.paths import AppPaths, ensure_within
from wiredeck.ps_runner import CREATE_NO_WINDOW, PsResult, run_powershell
from wiredeck.win32_helpers import Win32Ops, default_win32_ops

SUPPORTED_VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mp4",
    ".mkv",
    ".mov",
    ".flv",
    ".ts",
    ".m4v",
    ".webm",
    ".avi",
    ".wmv",
)

BACKEND_DEFAULT_HOTKEYS: dict[str, tuple[str, str]] = {
    "nvidia": ("Alt+F9", "Alt+F10"),
    "gamebar": ("Win+Alt+R", "Win+Alt+G"),
    "obs": ("F9", "F10"),
    "custom": ("F9", "F10"),
    "ffmpeg": ("", ""),
}

SUPPORTED_FILENAME_TOKENS: tuple[str, ...] = ("{preset}", "{date}", "{time}", "{backend}", "{mode}", "{orig}")
FAT32_SAFE_SPLIT_MB = 3800
CRITICAL_FREE_BYTES = 512 * 1024 * 1024
_SEQ_PREFIX_RE = re.compile(r"^(\d+)_")
_ILLEGAL_WIN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')

STREAM_CLIENT_PROCESS_NAMES: dict[str, tuple[str, ...]] = {
    "xbox": ("xstreaming.exe", "xstreamingdesktop.exe", "xboxpcapp.exe", "xboxapp.exe", "greenlight.exe"),
    "playstation": ("chiaki.exe", "chiaki-ng.exe", "remoteplay.exe", "psremoteplay.exe"),
    "handheld": ("moonlight.exe", "steam.exe", "parsec.exe", "parsecd.exe"),
    "other": ("moonlight.exe", "parsec.exe", "parsecd.exe", "xstreaming.exe", "chiaki-ng.exe"),
    "switch": ("xstreaming.exe",),
}

_CAPTURE_CARD_KEYWORDS: tuple[str, ...] = (
    "capture",
    "hdmi",
    "usb video",
    "usb3",
    "elgato",
    "avermedia",
    "cam link",
    "ugreen",
    "mirabox",
    "ezcap",
    "ms21",
    "game capture",
    "live gamer",
    "magewell",
    "decklink",
)


def cleanup_stale_part_files(target_dir: Path, *, min_age_sec: float = 600.0) -> dict[str, Any]:
    """Remove orphaned `*.<video_ext>.part` temp files older than `min_age_sec`."""
    if not target_dir.is_dir():
        return {"cleaned_count": 0, "freed_mb": 0.0, "files": []}
    now = time.time()
    cleaned: list[str] = []
    freed = 0
    for item in target_dir.iterdir():
        try:
            if not item.is_file() or not item.name.lower().endswith(".part"):
                continue
            inner = Path(item.name[:-5])
            if inner.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
                continue
            st = item.stat()
            if min_age_sec > 0 and (now - st.st_mtime) < min_age_sec:
                continue
            sz = int(st.st_size)
            item.unlink()
            freed += sz
            cleaned.append(item.name)
        except OSError:
            continue
    return {"cleaned_count": len(cleaned), "freed_mb": round(freed / (1024**2), 2), "files": cleaned}


def detect_ffmpeg_audio_devices(
    ffmpeg_exe: str,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> list[str]:
    """Query DirectShow audio capture devices from FFmpeg (`-list_devices true -f dshow`)."""
    exe = (ffmpeg_exe or "").strip()
    if not exe:
        return []
    try:
        if runner is not None:
            proc = runner([exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
        else:
            proc = subprocess.run(
                [exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=4,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
    except Exception:
        return []

    raw_text = "\n".join(x for x in (getattr(proc, "stderr", "") or "", getattr(proc, "stdout", "") or "") if x)
    devices: list[str] = []
    in_audio_section = False
    for line in raw_text.splitlines():
        low = line.lower()
        if "directshow video devices" in low:
            in_audio_section = False
            continue
        if "directshow audio devices" in low:
            in_audio_section = True
            continue
        if "(audio)" in low or (in_audio_section and "alternative name" not in low):
            m = re.search(r'"([^"]+)"', line)
            if m:
                name = m.group(1).strip()
                if name and name not in devices:
                    devices.append(name)
    return devices


def _pick_preferred_audio_device(devices: list[str]) -> str:
    if not devices:
        return ""
    priority = ("virtual-audio-capturer", "stereo mix", "立体声混音", "voicemeeter", "cable output")
    for kw in priority:
        for dev in devices:
            if kw in dev.lower():
                return dev
    return devices[0]


def detect_ffmpeg_video_devices(
    ffmpeg_exe: str,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> list[dict[str, Any]]:
    """Query DirectShow video capture devices (HDMI/USB capture cards, webcams) from FFmpeg."""
    exe = (ffmpeg_exe or "").strip()
    if not exe:
        return []
    try:
        if runner is not None:
            proc = runner([exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
        else:
            proc = subprocess.run(
                [exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=4,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
    except Exception:
        return []

    raw_text = "\n".join(x for x in (getattr(proc, "stderr", "") or "", getattr(proc, "stdout", "") or "") if x)
    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    in_video_section = False
    for line in raw_text.splitlines():
        low = line.lower()
        if "directshow video devices" in low:
            in_video_section = True
            continue
        if "directshow audio devices" in low:
            in_video_section = False
            continue
        if "(video)" in low or (in_video_section and "alternative name" not in low):
            m = re.search(r'"([^"]+)"', line)
            if m:
                name = m.group(1).strip()
                if name and name not in seen:
                    seen.add(name)
                    is_card = any(kw in name.lower() for kw in _CAPTURE_CARD_KEYWORDS)
                    devices.append({"name": name, "is_capture_card": is_card})
    devices.sort(key=lambda d: (0 if d["is_capture_card"] else 1, d["name"].lower()))
    return devices


def _match_stream_window(cfg: AppConfig, windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    preset_procs = list(STREAM_CLIENT_PROCESS_NAMES.get(cfg.active_preset, ()))
    custom = (cfg.custom_client_path or "").strip()
    if custom:
        c_exe = Path(custom).name.lower()
        if c_exe and c_exe not in preset_procs:
            preset_procs.insert(0, c_exe)
    all_known = list(dict.fromkeys(
        preset_procs + [p for plist in STREAM_CLIENT_PROCESS_NAMES.values() for p in plist]
    ))

    for target_proc in all_known:
        for w in windows:
            if str(w.get("process_name") or "").lower() == target_proc:
                return w

    title_hints = ("xstreaming", "chiaki", "moonlight", "ps remote play", "remote play")
    for w in windows:
        tl = str(w.get("title") or "").lower()
        if any(h in tl for h in title_hints):
            return w
    return None


def _match_specific_window(title_want: str, exe_want: str, windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    tw = (title_want or "").strip().lower()
    ew = Path(exe_want.strip()).name.lower() if (exe_want or "").strip() else ""
    if not tw and not ew:
        return None
    for w in windows:
        if tw and str(w.get("title") or "").strip().lower() == tw:
            return w
    if ew:
        for w in windows:
            if str(w.get("process_name") or "").strip().lower() == ew:
                return w
    if tw:
        for w in windows:
            if tw in str(w.get("title") or "").strip().lower():
                return w
    return None


def resolve_capture_target(
    cfg: AppConfig,
    *,
    windows: list[dict[str, Any]] | None = None,
    video_devices: list[dict[str, Any]] | None = None,
    win32_ops: Win32Ops | None = None,
) -> dict[str, Any]:
    """Resolve what WireDeck should capture (stream window, chosen window, capture card, or desktop)."""
    rec = cfg.recorder
    scope = (rec.capture_scope or "stream_window").strip().lower()
    if scope not in VALID_CAPTURE_SCOPES:
        scope = "stream_window"

    ops = win32_ops or default_win32_ops()
    win_list = windows if windows is not None else (ops.list_windows() if ops.list_windows else [])

    if scope == "desktop":
        return {
            "scope": scope,
            "effective_scope": "desktop",
            "ready": True,
            "fell_back_to_desktop": False,
            "hwnd": 0,
            "window_title": "",
            "window_exe": "",
            "window_class": "",
            "is_minimized": False,
            "video_device": "",
            "label": "整个电脑屏幕",
            "warning": "",
        }

    if scope == "capture_card":
        devs = video_devices
        if devs is None:
            ff = _find_ffmpeg_exe(rec.ffmpeg_exe_path)
            devs = detect_ffmpeg_video_devices(ff) if ff else []
        chosen_dev = rec.capture_video_device.strip()
        if not chosen_dev and devs:
            cards = [d for d in devs if d.get("is_capture_card")]
            chosen_dev = str((cards[0] if cards else devs[0]).get("name") or "")
        if chosen_dev:
            return {
                "scope": scope,
                "effective_scope": "capture_card",
                "ready": True,
                "fell_back_to_desktop": False,
                "hwnd": 0,
                "window_title": "",
                "window_exe": "",
                "window_class": "",
                "is_minimized": False,
                "video_device": chosen_dev,
                "label": chosen_dev,
                "warning": "",
            }
        return {
            "scope": scope,
            "effective_scope": "blocked",
            "ready": False,
            "fell_back_to_desktop": False,
            "hwnd": 0,
            "window_title": "",
            "window_exe": "",
            "window_class": "",
            "is_minimized": False,
            "video_device": "",
            "label": "",
            "warning": "未检测到 HDMI/USB 采集卡或视频采集设备，请插好设备后刷新，或切换为串流窗口/桌面",
        }

    matched = (
        _match_stream_window(cfg, win_list)
        if scope == "stream_window"
        else _match_specific_window(rec.capture_window_title, rec.capture_window_exe, win_list)
    )
    if matched is not None:
        title = str(matched.get("title") or "")
        exe = str(matched.get("process_name") or "")
        cls_name = str(matched.get("class_name") or "")
        is_min = bool(matched.get("is_minimized"))
        return {
            "scope": scope,
            "effective_scope": "window",
            "ready": True,
            "fell_back_to_desktop": False,
            "hwnd": int(matched.get("hwnd") or 0),
            "window_title": title,
            "window_exe": exe,
            "window_class": cls_name,
            "is_minimized": is_min,
            "video_device": "",
            "label": title or exe,
            "warning": "目标窗口目前处于最小化状态；开录时会自动唤醒到后台（请勿关闭）" if is_min else "",
        }

    if rec.strict_window_only:
        msg = (
            "未检测到正在运行的串流客户端窗口（已开启防误录桌面保护）"
            if scope == "stream_window"
            else "未找到指定的目标窗口（已开启防误录桌面保护）"
        )
        return {
            "scope": scope,
            "effective_scope": "blocked",
            "ready": False,
            "fell_back_to_desktop": False,
            "hwnd": 0,
            "window_title": rec.capture_window_title,
            "window_exe": rec.capture_window_exe,
            "window_class": "",
            "is_minimized": False,
            "video_device": "",
            "label": rec.capture_window_title or "目标窗口未打开",
            "warning": msg,
        }

    return {
        "scope": scope,
        "effective_scope": "desktop",
        "ready": True,
        "fell_back_to_desktop": True,
        "hwnd": 0,
        "window_title": "",
        "window_exe": "",
        "window_class": "",
        "is_minimized": False,
        "video_device": "",
        "label": "桌面兜底（串流窗口未打开）" if scope == "stream_window" else "桌面兜底（目标窗口未打开）",
        "warning": "暂未打开目标游戏窗口，开录将暂时拍全屏（开启串流或勾选防误录可锁定纯游戏画面）",
    }


def get_next_sequence_index(
    target_dir: Path | str,
    *,
    extensions: tuple[str, ...] | None = None,
) -> int:
    """Scan `target_dir` for captured files with leading `<n>_` prefix and return `max + 1` (min 1)."""
    raw = str(target_dir or "").strip()
    if not raw:
        return 1
    root = Path(raw)
    if not root.is_dir():
        return 1
    exts = tuple(e.lower() for e in (extensions or SUPPORTED_VIDEO_EXTENSIONS))
    max_idx = 0
    try:
        for item in root.iterdir():
            if not item.is_file() or item.suffix.lower() not in exts:
                continue
            m = _SEQ_PREFIX_RE.match(item.name)
            if m:
                try:
                    max_idx = max(max_idx, int(m.group(1)))
                except ValueError:
                    pass
    except OSError:
        return 1
    return max_idx + 1


def sanitize_filename_body(raw: str) -> str:
    """Clean custom naming body while stripping any `{seq}` token or leading `N_` attempt."""
    txt = str(raw or "").strip()
    txt = re.sub(r"\{seq\}_*", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"^\d+_+", "", txt)
    txt = _ILLEGAL_WIN_CHARS_RE.sub("_", txt)
    txt = re.sub(r"\s+", "_", txt)
    txt = re.sub(r"_+", "_", txt).strip("._- ")
    return txt[:96].strip("._- ")


def render_clip_filename(
    seq: int,
    template: str,
    *,
    ext: str,
    preset: str = "xbox",
    backend: str = "obs",
    mode: str = "replay",
    orig_stem: str = "",
    now: datetime | None = None,
) -> str:
    """Render filename with a MANDATORY leading `<seq>_` prefix (`1_`, `2_`, `3_`, ...)."""
    seq_num = max(1, int(seq))
    ts = now or datetime.now()
    clean_orig = sanitize_filename_body(orig_stem) or "clip"
    tokens = {
        "preset": sanitize_filename_body(preset) or "device",
        "date": ts.strftime("%Y%m%d"),
        "time": ts.strftime("%H%M%S"),
        "backend": sanitize_filename_body(backend) or "rec",
        "mode": sanitize_filename_body(mode) or "clip",
        "orig": clean_orig,
    }

    tpl = str(template or "").strip() or DEFAULT_FILENAME_TEMPLATE
    rendered = tpl
    for k, v in tokens.items():
        rendered = re.sub(rf"\{{{k}\}}", lambda _m, val=v: val, rendered, flags=re.IGNORECASE)
    rendered = re.sub(r"\{[a-z0-9_]+\}", "", rendered, flags=re.IGNORECASE)
    body = sanitize_filename_body(rendered)
    if not body:
        fallback = DEFAULT_FILENAME_TEMPLATE.format(preset=tokens["preset"], date=tokens["date"], time=tokens["time"])
        body = sanitize_filename_body(fallback) or f"{tokens['preset']}_{tokens['date']}_{tokens['time']}"

    clean_ext = re.sub(r"[^a-z0-9]", "", str(ext or "mkv").lower()) or "mkv"
    return f"{seq_num}_{body}.{clean_ext}"


def preview_clip_filenames(
    template: str,
    *,
    target_dir: str = "",
    ext: str = "mkv",
    preset: str = "xbox",
    backend: str = "obs",
    mode: str = "replay",
    count: int = 3,
) -> list[str]:
    """Generate consecutive live preview filenames starting from the folder's next sequence index."""
    start_seq = get_next_sequence_index(target_dir)
    base_dt = datetime.now()
    return [
        render_clip_filename(
            start_seq + i,
            template,
            ext=ext,
            preset=preset,
            backend=backend,
            mode=mode,
            orig_stem="clip",
            now=base_dt,
        )
        for i in range(max(1, int(count)))
    ]


def rename_clip_with_forced_seq(target_dir: Path, clip_path: Path, new_body: str) -> ActionResult:
    """Rename a clip strictly inside `target_dir` while enforcing its leading `N_` sequence prefix."""
    try:
        resolved = ensure_within(target_dir, clip_path)
    except ValueError as exc:
        return ActionResult(ok=False, code="PATH_ESCAPE_BLOCKED", message=str(exc), exit_code=1)
    if resolved.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        return ActionResult(ok=False, code="NOT_A_VIDEO", message="安全限制：仅允许重命名受支持的录像文件", exit_code=1)
    if not resolved.is_file():
        return ActionResult(ok=False, code="NOT_FOUND", message="录像文件不存在", exit_code=1)

    m = _SEQ_PREFIX_RE.match(resolved.name)
    seq = int(m.group(1)) if m else get_next_sequence_index(target_dir)

    raw_new = str(new_body or "").strip()
    if raw_new.lower().endswith(resolved.suffix.lower()):
        raw_new = raw_new[: -len(resolved.suffix)]
    clean_body = sanitize_filename_body(raw_new)
    if not clean_body:
        return ActionResult(ok=False, code="INVALID_NAME", message="请输入有效的文件名（最前方的序号将自动保留）", exit_code=1)

    dest_name = f"{seq}_{clean_body}{resolved.suffix}"
    dest = resolved.parent / dest_name
    if dest != resolved:
        dest = _unique_dest_path(resolved.parent, Path(dest_name))
        resolved.rename(dest)
    return ActionResult(
        ok=True,
        code="CLIP_RENAMED",
        message=f"已重命名为：{dest.name}",
        details={"name": dest.name, "path": str(dest), "seq": seq},
    )


def resolve_effective_split_policy(
    split_mode: str,
    split_minutes: int,
    split_size_mb: int,
    *,
    filesystem: str = "",
) -> dict[str, Any]:
    """Normalize recording split thresholds and enforce FAT32 <= 3800 MB single-file safeguard."""
    mode = str(split_mode or "off").strip().lower()
    if mode not in VALID_SPLIT_MODES:
        mode = "off"
    mins = max(1, min(240, int(split_minutes or 30)))
    size_mb = max(50, min(32768, int(split_size_mb or 2048)))
    fat32_guarded = False

    if str(filesystem or "").strip().upper() == "FAT32":
        if mode == "off":
            mode = "size"
            size_mb = FAT32_SAFE_SPLIT_MB
            fat32_guarded = True
        elif mode == "time":
            mode = "both"
            size_mb = FAT32_SAFE_SPLIT_MB
            fat32_guarded = True
        elif size_mb > FAT32_SAFE_SPLIT_MB:
            size_mb = FAT32_SAFE_SPLIT_MB
            fat32_guarded = True

    time_sec = mins * 60 if mode in ("time", "both") else 0
    size_bytes = size_mb * 1024 * 1024 if mode in ("size", "both") else 0
    return {
        "mode": mode,
        "split_minutes": mins,
        "split_size_mb": size_mb,
        "time_sec": time_sec,
        "size_bytes": size_bytes,
        "fat32_guarded": fat32_guarded,
    }


def resolve_recording_dir(
    output_dir: str,
    fallback_dir: str = "",
    *,
    bitrate_mbps: int = 10,
    min_free_gb: float = 5.0,
    drives: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve active recording directory with transparent failover to `fallback_dir` if USB is unplugged."""
    primary_raw = (output_dir or "").strip()
    fallback_raw = (fallback_dir or "").strip()
    drive_list = drives if drives is not None else list_storage_drives(bitrate_mbps=bitrate_mbps)

    if primary_raw:
        primary_info = inspect_target_path(primary_raw, bitrate_mbps=bitrate_mbps, min_free_gb=min_free_gb, drives=drive_list)
        if primary_info.get("online"):
            return {**primary_info, "used_fallback": False}
        if fallback_raw:
            fb_info = inspect_target_path(fallback_raw, bitrate_mbps=bitrate_mbps, min_free_gb=min_free_gb, drives=drive_list)
            if fb_info.get("online"):
                return {**fb_info, "used_fallback": True, "primary_offline_path": primary_raw}
        return {**primary_info, "used_fallback": False}

    if fallback_raw:
        fb_info = inspect_target_path(fallback_raw, bitrate_mbps=bitrate_mbps, min_free_gb=min_free_gb, drives=drive_list)
        return {**fb_info, "used_fallback": False}

    default_dir = str(Path.home() / "Videos" / "WireDeck")
    def_info = inspect_target_path(default_dir, bitrate_mbps=bitrate_mbps, min_free_gb=min_free_gb, drives=drive_list)
    return {**def_info, "used_fallback": False}


def _estimate_hours(free_bytes: int, bitrate_mbps: int) -> float:
    mbps = max(1, int(bitrate_mbps or 10))
    bytes_per_hour = (mbps * 1_000_000 / 8.0) * 3600.0
    if free_bytes <= 0 or bytes_per_hour <= 0:
        return 0.0
    return round(free_bytes / bytes_per_hour, 1)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(512 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _drive_type_label(dtype: int) -> str:
    return {
        2: "USB 可移动盘",
        3: "本机固定盘",
        4: "网络驱动器",
    }.get(int(dtype), "其他磁盘")


def list_storage_drives(
    *,
    runner: Callable[[str, int], PsResult] = run_powershell,
    bitrate_mbps: int = 10,
) -> list[dict[str, Any]]:
    """List mounted Windows logical drives with filesystem, free GB, and recording capacity."""
    ps = r"""
$disks = @(Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue | Where-Object { $_.DriveType -in @(2, 3, 4) } | ForEach-Object {
    [PSCustomObject]@{
        device_id = [string]$_.DeviceID
        volume_name = [string]$_.VolumeName
        drive_type = [int]$_.DriveType
        filesystem = [string]$_.FileSystem
        free_bytes = [int64]($_.FreeSpace | ForEach-Object { if ($_) { $_ } else { 0 } })
        total_bytes = [int64]($_.Size | ForEach-Object { if ($_) { $_ } else { 0 } })
    }
})
$disks | ConvertTo-Json -Depth 3 -Compress
"""
    raw_items: list[dict[str, Any]] = []
    res = runner(ps, 8)
    if res.returncode == 0 and res.stdout.strip():
        try:
            parsed = json.loads(res.stdout.strip())
            if isinstance(parsed, dict):
                raw_items = [parsed]
            elif isinstance(parsed, list):
                raw_items = [x for x in parsed if isinstance(x, dict)]
        except Exception:
            pass

    drives: list[dict[str, Any]] = []
    for item in raw_items:
        dev = str(item.get("device_id") or "").strip().upper()
        if not dev:
            continue
        fs = str(item.get("filesystem") or "").strip()
        dtype = int(item.get("drive_type") or 0)
        free_b = max(0, int(item.get("free_bytes") or 0))
        total_b = max(0, int(item.get("total_bytes") or 0))
        free_gb = round(free_b / (1024**3), 2)
        total_gb = round(total_b / (1024**3), 2)
        supports_large = fs.upper() != "FAT32"
        is_removable = dtype == 2
        drives.append(
            {
                "device_id": dev,
                "volume_name": str(item.get("volume_name") or "").strip(),
                "drive_type": dtype,
                "drive_type_label": _drive_type_label(dtype),
                "filesystem": fs or "Unknown",
                "free_bytes": free_b,
                "total_bytes": total_b,
                "free_gb": free_gb,
                "total_gb": total_gb,
                "supports_large_files": supports_large,
                "est_hours": _estimate_hours(free_b, bitrate_mbps),
                "is_removable": is_removable,
                "recommended": False,
            }
        )

    # Prefer removable exFAT/NTFS with >=10GB free, otherwise non-C or largest free space
    rem_cands = [d for d in drives if d["is_removable"] and d["supports_large_files"] and d["free_gb"] >= 10.0]
    if rem_cands:
        best = max(rem_cands, key=lambda d: d["free_gb"])
        best["recommended"] = True
    elif drives:
        non_c = [d for d in drives if d["device_id"] != "C:" and d["supports_large_files"]]
        pool = non_c or drives
        best = max(pool, key=lambda d: d["free_gb"])
        best["recommended"] = True

    return drives


def inspect_target_path(
    output_dir: str,
    *,
    bitrate_mbps: int = 10,
    min_free_gb: float = 5.0,
    drives: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Inspect configured output directory's mount state, filesystem, free space, and FAT32 guard."""
    raw = (output_dir or "").strip()
    if not raw:
        return {
            "configured": False,
            "online": False,
            "path": "",
            "filesystem": "",
            "free_gb": 0.0,
            "est_hours": 0.0,
            "warnings": ["尚未设置目标录像目录，建议设为外接 U盘或空余空间充裕的分区"],
        }

    p = Path(raw)
    drive_str = p.drive.upper() if p.drive else ""
    drive_list = drives if drives is not None else list_storage_drives(bitrate_mbps=bitrate_mbps)
    matched = next((d for d in drive_list if d["device_id"] == drive_str), None) if drive_str else None

    if drive_str and not Path(drive_str + "\\").exists() and matched is None:
        return {
            "configured": True,
            "online": False,
            "path": raw,
            "device_id": drive_str,
            "filesystem": "",
            "free_gb": 0.0,
            "est_hours": 0.0,
            "warnings": [f"未检测到目标磁盘 {drive_str}（外接 U盘可能未插入）"],
        }

    free_b = 0
    try:
        probe_target = p if p.exists() else (Path(drive_str + "\\") if drive_str else p.parent)
        if probe_target.exists():
            free_b = int(shutil.disk_usage(probe_target).free)
    except OSError:
        if matched:
            free_b = int(matched.get("free_bytes") or 0)

    if matched and free_b == 0:
        free_b = int(matched.get("free_bytes") or 0)

    free_gb = round(free_b / (1024**3), 2)
    fs = str(matched.get("filesystem") or "") if matched else ""
    warnings: list[str] = []
    if fs.upper() == "FAT32":
        warnings.append("目标盘为 FAT32 格式，单个视频文件不能超过 4 GB；建议分段录制或使用 exFAT/NTFS")
    if free_gb < max(0.5, float(min_free_gb or 5.0)):
        warnings.append(f"目标盘剩余空间仅 {free_gb} GB，低于警戒阈值 {min_free_gb} GB")

    return {
        "configured": True,
        "online": True,
        "path": raw,
        "device_id": drive_str,
        "volume_name": str(matched.get("volume_name") or "") if matched else "",
        "filesystem": fs or "Local",
        "supports_large_files": fs.upper() != "FAT32",
        "is_removable": bool(matched.get("is_removable")) if matched else False,
        "free_gb": free_gb,
        "est_hours": _estimate_hours(free_b, bitrate_mbps),
        "warnings": warnings,
    }


def detect_obs_info(
    custom_exe: str = "",
    *,
    appdata_dir: Path | None = None,
) -> dict[str, Any]:
    """Detect OBS Studio executable and inspect active profile's native format & output path."""
    exe = ""
    cands: list[Path] = []
    if custom_exe.strip():
        cands.append(Path(custom_exe.strip()))
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pfx = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    cands.extend(
        [
            pf / "obs-studio" / "bin" / "64bit" / "obs64.exe",
            pfx / "obs-studio" / "bin" / "64bit" / "obs64.exe",
            Path(r"C:\Program Files (x86)\Steam\steamapps\common\OBS Studio\bin\64bit\obs64.exe"),
            Path(r"D:\SteamLibrary\steamapps\common\OBS Studio\bin\64bit\obs64.exe"),
        ]
    )
    for c in cands:
        try:
            if c.is_file():
                exe = str(c)
                break
        except OSError:
            continue

    base_appdata = appdata_dir or (Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else (Path.home() / "AppData" / "Roaming"))
    obs_root = base_appdata / "obs-studio"
    profile_name = ""
    basic_ini: Path | None = None

    global_ini = obs_root / "global.ini"
    if global_ini.is_file():
        try:
            cp = configparser.RawConfigParser()
            cp.optionxform = str  # type: ignore[assignment]
            cp.read(global_ini, encoding="utf-8-sig")
            if cp.has_section("Basic") and cp.has_option("Basic", "ProfileDir"):
                profile_name = cp.get("Basic", "ProfileDir").strip()
            elif cp.has_section("Basic") and cp.has_option("Basic", "Profile"):
                profile_name = cp.get("Basic", "Profile").strip()
        except Exception:
            pass

    profiles_dir = obs_root / "basic" / "profiles"
    if profile_name and (profiles_dir / profile_name / "basic.ini").is_file():
        basic_ini = profiles_dir / profile_name / "basic.ini"
    elif profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir(), key=lambda p: p.name.lower()):
            if (child / "basic.ini").is_file():
                profile_name = child.name
                basic_ini = child / "basic.ini"
                break

    rec_path = ""
    rec_format = ""
    output_mode = "Simple"
    split_enabled = False
    split_type = ""
    split_time = 0
    split_size = 0
    if basic_ini and basic_ini.is_file():
        try:
            bcp = configparser.RawConfigParser()
            bcp.optionxform = str  # type: ignore[assignment]
            bcp.read(basic_ini, encoding="utf-8-sig")
            if bcp.has_section("Output") and bcp.has_option("Output", "Mode"):
                output_mode = bcp.get("Output", "Mode").strip() or "Simple"
            if bcp.has_section("AdvOut"):
                split_enabled = bcp.get("AdvOut", "RecSplitFile", fallback="false").strip().lower() in ("true", "1", "yes")
                split_type = bcp.get("AdvOut", "RecSplitFileType", fallback="").strip()
                try:
                    split_time = int(bcp.get("AdvOut", "RecSplitFileTime", fallback="0") or 0)
                except ValueError:
                    split_time = 0
                try:
                    split_size = int(bcp.get("AdvOut", "RecSplitFileSize", fallback="0") or 0)
                except ValueError:
                    split_size = 0
            if output_mode.lower() == "advanced" and bcp.has_section("AdvOut"):
                rec_path = bcp.get("AdvOut", "RecFilePath", fallback="").strip()
                rec_format = bcp.get("AdvOut", "RecFormat2", fallback=bcp.get("AdvOut", "RecFormat", fallback="")).strip()
            if not rec_path and bcp.has_section("SimpleOutput"):
                rec_path = bcp.get("SimpleOutput", "FilePath", fallback="").strip()
            if not rec_format and bcp.has_section("SimpleOutput"):
                rec_format = bcp.get("SimpleOutput", "RecFormat2", fallback=bcp.get("SimpleOutput", "RecFormat", fallback="")).strip()
        except Exception:
            pass

    return {
        "installed": bool(exe),
        "exe_path": exe,
        "profile_name": profile_name,
        "basic_ini": str(basic_ini) if basic_ini else "",
        "output_mode": output_mode,
        "record_path": rec_path,
        "record_format": rec_format,
        "split_enabled": split_enabled,
        "split_type": split_type,
        "split_time": split_time,
        "split_size": split_size,
    }


def _format_obs_window_string(title: str, class_name: str, exe_name: str) -> str:
    def _esc(s: str) -> str:
        return (s or "").replace(":", "#3A")

    return f"{_esc(title)}:{_esc(class_name)}:{_esc(exe_name)}"


def sync_obs_capture_source(
    capture_target: dict[str, Any],
    *,
    paths: AppPaths,
    appdata_dir: Path | None = None,
) -> dict[str, Any]:
    """Configure WireDeck-managed OBS WGC Window / Capture Card / Display source without removing user sources."""
    base_appdata = appdata_dir or (Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else (Path.home() / "AppData" / "Roaming"))
    obs_root = base_appdata / "obs-studio"
    global_ini = obs_root / "global.ini"
    scene_file: Path | None = None

    if global_ini.is_file():
        try:
            cp = configparser.RawConfigParser()
            cp.optionxform = str  # type: ignore[assignment]
            cp.read(global_ini, encoding="utf-8-sig")
            sc_name = cp.get("Basic", "SceneCollectionFile", fallback="").strip()
            if sc_name:
                cand = obs_root / "basic" / "scenes" / f"{sc_name}.json"
                if cand.is_file():
                    scene_file = cand
        except Exception:
            pass

    if scene_file is None:
        scenes_dir = obs_root / "basic" / "scenes"
        if scenes_dir.is_dir():
            for child in sorted(scenes_dir.glob("*.json")):
                scene_file = child
                break

    if scene_file is None or not scene_file.is_file():
        return {"synced": False, "reason": "no_scene_file"}

    try:
        scene_data = json.loads(scene_file.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"synced": False, "reason": "invalid_scene_json"}
    if not isinstance(scene_data, dict):
        return {"synced": False, "reason": "invalid_scene_root"}

    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    backup = paths.runtime_dir / "obs_scene_backup.json"
    if not backup.is_file():
        shutil.copy2(scene_file, backup)

    eff = str(capture_target.get("effective_scope") or "desktop")
    source_name = "WireDeck Capture"
    if eff == "window":
        win_str = _format_obs_window_string(
            str(capture_target.get("window_title") or ""),
            str(capture_target.get("window_class") or ""),
            str(capture_target.get("window_exe") or ""),
        )
        src_id = "window_capture"
        src_settings: dict[str, Any] = {
            "method": 2,  # WGC: captures occluded background windows cleanly without showing overlays
            "priority": 2,  # Match title, otherwise window of same executable
            "window": win_str,
            "cursor": False,
            "client_area": True,
        }
    elif eff == "capture_card" and capture_target.get("video_device"):
        src_id = "dshow_input"
        src_settings = {
            "video_device_id": str(capture_target.get("video_device") or ""),
        }
    else:
        src_id = "monitor_capture"
        src_settings = {"method": 0}

    sources = scene_data.get("sources")
    if not isinstance(sources, list):
        sources = []
        scene_data["sources"] = sources

    existing_src = next((s for s in sources if isinstance(s, dict) and s.get("name") == source_name), None)
    if existing_src is not None:
        existing_src["id"] = src_id
        existing_src["settings"] = src_settings
    else:
        sources.append(
            {
                "name": source_name,
                "id": src_id,
                "settings": src_settings,
                "enabled": True,
            }
        )

    cur_scene = str(scene_data.get("current_scene") or "")
    scene_obj = next(
        (s for s in sources if isinstance(s, dict) and s.get("id") == "scene" and (not cur_scene or s.get("name") == cur_scene)),
        None,
    )
    if scene_obj is not None:
        sc_settings = scene_obj.setdefault("settings", {})
        items = sc_settings.setdefault("items", [])
        if not any(isinstance(it, dict) and it.get("name") == source_name for it in items):
            items.append({"name": source_name, "visible": True})

    tmp_scene = scene_file.with_suffix(".json.wiredeck.tmp")
    tmp_scene.write_text(json.dumps(scene_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_scene, scene_file)
    return {"synced": True, "source_id": src_id, "scene_file": str(scene_file)}


def sync_obs_output_dir(
    target_dir: str,
    *,
    paths: AppPaths,
    appdata_dir: Path | None = None,
    split_mode: str = "off",
    split_minutes: int = 30,
    split_size_mb: int = 2048,
    capture_target: dict[str, Any] | None = None,
) -> ActionResult:
    """Safely point OBS Studio's output folder, auto-split, and WireDeck Capture source."""
    out_raw = (target_dir or "").strip()
    if not out_raw:
        return ActionResult(ok=False, code="NO_OUTPUT_DIR", message="请先设置目标录像文件夹", exit_code=1)

    obs = detect_obs_info(appdata_dir=appdata_dir)
    ini_raw = obs.get("basic_ini") or ""
    if not ini_raw or not Path(ini_raw).is_file():
        return ActionResult(
            ok=False,
            code="OBS_INI_MISSING",
            message="未检测到 OBS 配置文件（请先启动并配置过一次 OBS Studio）",
            exit_code=1,
        )

    ini_path = Path(ini_raw)
    Path(out_raw).mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    backup = paths.runtime_dir / "obs_basic_ini_backup.ini"
    if not backup.is_file():
        shutil.copy2(ini_path, backup)

    cp = configparser.RawConfigParser()
    cp.optionxform = str  # type: ignore[assignment]
    cp.read(ini_path, encoding="utf-8-sig")

    norm_dir = str(Path(out_raw))
    if not cp.has_section("SimpleOutput"):
        cp.add_section("SimpleOutput")
    cp.set("SimpleOutput", "FilePath", norm_dir)

    if not cp.has_section("AdvOut"):
        cp.add_section("AdvOut")
    cp.set("AdvOut", "RecFilePath", norm_dir)

    policy = resolve_effective_split_policy(split_mode, split_minutes, split_size_mb)
    p_mode = policy["mode"]
    if p_mode == "off":
        cp.set("AdvOut", "RecSplitFile", "false")
    elif p_mode == "size":
        cp.set("AdvOut", "RecSplitFile", "true")
        cp.set("AdvOut", "RecSplitFileType", "Size")
        cp.set("AdvOut", "RecSplitFileSize", str(policy["split_size_mb"]))
    elif p_mode == "time":
        cp.set("AdvOut", "RecSplitFile", "true")
        cp.set("AdvOut", "RecSplitFileType", "Time")
        cp.set("AdvOut", "RecSplitFileTime", str(policy["split_minutes"]))
    else:  # both
        cp.set("AdvOut", "RecSplitFile", "true")
        cp.set("AdvOut", "RecSplitFileType", "Time")
        cp.set("AdvOut", "RecSplitFileTime", str(policy["split_minutes"]))
        cp.set("AdvOut", "RecSplitFileSize", str(policy["split_size_mb"]))

    tmp_path = ini_path.with_suffix(".ini.wiredeck.tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="\r\n") as fh:
        cp.write(fh, space_around_delimiters=False)
    os.replace(tmp_path, ini_path)

    split_note = ""
    if p_mode == "time":
        split_note = f"，并开启每 {policy['split_minutes']} 分钟自动分段"
    elif p_mode == "size":
        split_note = f"，并开启每 {policy['split_size_mb']} MB 自动分段"
    elif p_mode == "both":
        split_note = f"，并开启 {policy['split_minutes']} 分钟 / {policy['split_size_mb']} MB 自动分段"

    src_sync: dict[str, Any] = {"synced": False}
    src_note = ""
    if capture_target is not None:
        src_sync = sync_obs_capture_source(capture_target, paths=paths, appdata_dir=appdata_dir)
        if src_sync.get("synced"):
            if src_sync.get("source_id") == "window_capture":
                src_note = "；已同步 WGC 窗口后台防遮挡采集源"
            elif src_sync.get("source_id") == "dshow_input":
                src_note = "；已同步采集卡输入源"
            else:
                src_note = "；已同步显示器采集源"

    return ActionResult(
        ok=True,
        code="OBS_SYNCED",
        message=f"已将 OBS 录像输出目录同步至 {norm_dir}{split_note}{src_note}（保留原编码与封装格式）",
        details={
            "basic_ini": str(ini_path),
            "output_dir": norm_dir,
            "record_format": obs.get("record_format", ""),
            "split_mode": p_mode,
            "scene_sync": src_sync,
        },
    )


def _find_ffmpeg_exe(custom_path: str = "") -> str:
    if custom_path.strip() and Path(custom_path.strip()).is_file():
        return custom_path.strip()
    found = shutil.which("ffmpeg")
    if found and Path(found).is_file():
        return found
    for cand in (
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
    ):
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return ""


def _default_watch_dirs() -> list[Path]:
    home = Path.home()
    cands = [
        home / "Videos" / "Captures",
        home / "Videos" / "NVIDIA",
        home / "Videos",
    ]
    return [p for p in cands if p.is_dir()]


def detect_recorders(cfg: AppConfig, *, system_env: dict[str, Any] | None = None) -> dict[str, Any]:
    """Detect available external & built-in recording backends cleanly."""
    rec = cfg.recorder
    obs = detect_obs_info(rec.obs_exe_path)
    nv_gpu = (system_env or {}).get("primary_nvidia_gpu")
    nv_app = bool((system_env or {}).get("nvidia_app_installed")) or (nv_gpu is not None)
    ffmpeg_exe = _find_ffmpeg_exe(rec.ffmpeg_exe_path)
    custom_ok = bool(rec.custom_exe_path.strip() and Path(rec.custom_exe_path.strip()).is_file())

    if rec.backend != "auto":
        effective = rec.backend
    elif obs["installed"]:
        effective = "obs"
    elif nv_app:
        effective = "nvidia"
    elif ffmpeg_exe:
        effective = "ffmpeg"
    else:
        effective = "gamebar"

    return {
        "effective_backend": effective,
        "obs": obs,
        "nvidia": {
            "available": nv_app,
            "supports_av1": bool(nv_gpu and nv_gpu.get("supports_smooth_motion")),
            "gpu_name": str((nv_gpu or {}).get("name") or ""),
        },
        "gamebar": {"available": os.name == "nt"},
        "ffmpeg": {"available": bool(ffmpeg_exe), "exe_path": ffmpeg_exe},
        "custom": {"available": custom_ok, "exe_path": rec.custom_exe_path},
        "watch_dirs": [str(p) for p in _default_watch_dirs()],
    }


def list_recorded_clips(target_dir: str, *, limit: int = 200) -> dict[str, Any]:
    """Scan `target_dir` for all supported video formats without filtering or altering native files."""
    raw = (target_dir or "").strip()
    if not raw:
        return {"ok": True, "clips": [], "total_count": 0, "total_bytes": 0, "total_gb": 0.0}
    root = Path(raw)
    if not root.is_dir():
        return {"ok": True, "clips": [], "total_count": 0, "total_bytes": 0, "total_gb": 0.0}

    clips: list[dict[str, Any]] = []
    total_bytes = 0
    for item in root.iterdir():
        try:
            if not item.is_file() or item.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
                continue
            st = item.stat()
            total_bytes += int(st.st_size)
            seq_m = _SEQ_PREFIX_RE.match(item.name)
            clips.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "ext": item.suffix.lower().lstrip("."),
                    "seq": int(seq_m.group(1)) if seq_m else 0,
                    "size_bytes": int(st.st_size),
                    "size_mb": round(st.st_size / (1024**2), 1),
                    "mtime_ts": float(st.st_mtime),
                    "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"),
                }
            )
        except OSError:
            continue

    clips.sort(key=lambda c: c["mtime_ts"], reverse=True)
    return {
        "ok": True,
        "clips": clips[: max(1, int(limit))],
        "total_count": len(clips),
        "total_bytes": total_bytes,
        "total_gb": round(total_bytes / (1024**3), 2),
    }


def _unique_dest_path(target_dir: Path, src: Path) -> Path:
    cand = target_dir / src.name
    if not cand.exists():
        return cand
    idx = 1
    while True:
        alt = target_dir / f"{src.stem}_{idx}{src.suffix}"
        if not alt.exists():
            return alt
        idx += 1


def offload_clips_to_target(
    source_dirs: list[Path],
    target_dir: Path,
    *,
    since_ts: float = 0.0,
    delete_source: bool = True,
    min_age_sec: float = 2.0,
    apply_naming: bool = False,
    filename_template: str = "",
    preset: str = "xbox",
    backend: str = "obs",
    mode: str = "replay",
) -> ActionResult:
    """Safely offload videos from local capture folders to `target_dir` with size + SHA-256 verification."""
    target_resolved = target_dir.resolve()
    target_resolved.mkdir(parents=True, exist_ok=True)
    try:
        cleanup_stale_part_files(target_resolved)
    except OSError:
        pass
    now = time.time()
    moved: list[dict[str, Any]] = []
    skipped = 0
    next_seq = get_next_sequence_index(target_resolved) if apply_naming else 1

    for sdir in source_dirs:
        if not sdir.is_dir() or sdir.resolve() == target_resolved:
            continue
        for item in sorted(sdir.rglob("*"), key=lambda p: p.stat().st_mtime if p.exists() else 0):
            try:
                if not item.is_file() or item.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
                    continue
                st = item.stat()
                if since_ts > 0 and st.st_mtime < since_ts:
                    continue
                if min_age_sec > 0 and (now - st.st_mtime) < min_age_sec:
                    skipped += 1
                    continue

                if apply_naming:
                    named = render_clip_filename(
                        next_seq,
                        filename_template,
                        ext=item.suffix.lstrip("."),
                        preset=preset,
                        backend=backend,
                        mode=mode,
                        orig_stem=item.stem,
                        now=datetime.fromtimestamp(st.st_mtime),
                    )
                    dest = _unique_dest_path(target_resolved, Path(named))
                else:
                    dest = _unique_dest_path(target_resolved, item)
                part = dest.with_suffix(dest.suffix + ".part")
                src_sha = _sha256_file(item)
                shutil.copy2(item, part)
                if part.stat().st_size != st.st_size or _sha256_file(part) != src_sha:
                    part.unlink(missing_ok=True)
                    raise IOError(f"Checksum mismatch while offloading {item.name}")
                os.replace(part, dest)
                if apply_naming:
                    next_seq += 1
                if delete_source:
                    item.unlink(missing_ok=True)
                moved.append({"name": dest.name, "dest": str(dest), "size_bytes": int(st.st_size)})
            except Exception:
                continue

    if not moved:
        return ActionResult(
            ok=True,
            code="OFFLOAD_NONE",
            message="暂无需搬运的新录像片段（原目录已清空或文件仍在写入中）",
            details={"moved_count": 0, "skipped_active": skipped},
        )

    total_mb = round(sum(m["size_bytes"] for m in moved) / (1024**2), 1)
    return ActionResult(
        ok=True,
        code="OFFLOAD_DONE",
        message=f"已通过 SHA-256 校验搬运 {len(moved)} 个片段（共 {total_mb} MB）到 {target_resolved}",
        details={"moved_count": len(moved), "total_mb": total_mb, "files": moved},
    )


def prune_clips_by_quota(
    target_dir: Path,
    *,
    max_storage_gb: float = 0.0,
    min_free_gb: float = 5.0,
    free_bytes_fn: Callable[[Path], int] | None = None,
    exclude_paths: set[Path] | None = None,
) -> ActionResult:
    """Safely prune oldest video files strictly inside `target_dir` to honor quota or min-free floor."""
    if not target_dir.is_dir():
        return ActionResult(ok=True, code="PRUNE_SKIPPED", message="目标目录不存在，跳过配额清理", details={"deleted_count": 0})
    try:
        cleanup_stale_part_files(target_dir)
    except OSError:
        pass

    excluded = {p.resolve() for p in (exclude_paths or set())}
    files: list[tuple[float, int, Path]] = []
    total_bytes = 0
    for item in target_dir.iterdir():
        try:
            if not item.is_file() or item.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
                continue
            if item.resolve() in excluded:
                continue
            st = item.stat()
            total_bytes += int(st.st_size)
            files.append((float(st.st_mtime), int(st.st_size), item))
        except OSError:
            continue

    files.sort(key=lambda x: x[0])  # Oldest first
    max_bytes = int(float(max_storage_gb) * (1024**3)) if float(max_storage_gb or 0) > 0 else 0
    min_free_bytes = int(float(min_free_gb) * (1024**3)) if float(min_free_gb or 0) > 0 else 0
    get_free = free_bytes_fn or (lambda p: int(shutil.disk_usage(p).free))
    cur_free = get_free(target_dir)

    deleted: list[str] = []
    freed_bytes = 0
    for _, fsize, fpath in files:
        over_quota = max_bytes > 0 and total_bytes > max_bytes
        under_free = min_free_bytes > 0 and cur_free < min_free_bytes
        if not over_quota and not under_free:
            break
        try:
            fpath.unlink()
            total_bytes = max(0, total_bytes - fsize)
            cur_free += fsize
            freed_bytes += fsize
            deleted.append(fpath.name)
        except OSError:
            continue

    if not deleted:
        return ActionResult(
            ok=True,
            code="PRUNE_OK",
            message="当前存储未触发配额或余量告警，无需清理",
            details={"deleted_count": 0, "freed_mb": 0.0},
        )
    freed_mb = round(freed_bytes / (1024**2), 1)
    return ActionResult(
        ok=True,
        code="PRUNE_DONE",
        message=f"已按时间顺序清理最旧的 {len(deleted)} 个录像（释放 {freed_mb} MB）",
        details={"deleted_count": len(deleted), "freed_mb": freed_mb, "deleted": deleted},
    )


def delete_clip_file(target_dir: Path, clip_path: Path) -> ActionResult:
    """Safely delete a single video clip strictly contained within `target_dir`."""
    try:
        resolved = ensure_within(target_dir, clip_path)
    except ValueError as exc:
        return ActionResult(ok=False, code="PATH_ESCAPE_BLOCKED", message=str(exc), exit_code=1)
    if resolved.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        return ActionResult(ok=False, code="NOT_A_VIDEO", message="安全限制：仅允许删除受支持的录像文件", exit_code=1)
    if not resolved.is_file():
        return ActionResult(ok=False, code="NOT_FOUND", message="录像文件不存在", exit_code=1)
    resolved.unlink()
    return ActionResult(ok=True, code="CLIP_DELETED", message=f"已删除录像：{resolved.name}")


def build_ffmpeg_command(
    ffmpeg_exe: str,
    output_file: Path,
    *,
    encoder: str = "auto",
    container_fmt: str = "mkv",
    bitrate_mbps: int = 10,
    fps: int = 60,
    system_env: dict[str, Any] | None = None,
    audio_device: str = "",
    capture_scope: str = "desktop",
    window_title: str = "",
    video_device: str = "",
) -> list[str]:
    """Build universal low-overhead FFmpeg capture command (desktop, targeted window, or capture card)."""
    enc = (encoder or "auto").strip().lower()
    if enc == "auto":
        nv = (system_env or {}).get("primary_nvidia_gpu")
        if nv and nv.get("supports_smooth_motion"):
            enc = "av1_nvenc"
        elif nv:
            enc = "hevc_nvenc"
        else:
            enc = "libx264"

    rate = max(2, min(60, int(bitrate_mbps or 10)))
    fps_val = 30 if int(fps or 60) <= 30 else 60
    clean_audio = (audio_device or "").strip()
    scope_norm = (capture_scope or "desktop").strip().lower()
    clean_win = (window_title or "").strip()
    clean_vdev = (video_device or "").strip()

    cmd = [ffmpeg_exe, "-y"]
    need_even_pad = False
    if scope_norm == "capture_card" and clean_vdev:
        cmd.extend(["-f", "dshow", "-framerate", str(fps_val), "-i", f"video={clean_vdev}"])
    elif scope_norm in ("stream_window", "window") and clean_win:
        cmd.extend(["-f", "gdigrab", "-framerate", str(fps_val), "-i", f"title={clean_win}"])
        need_even_pad = True
    else:
        cmd.extend(["-f", "gdigrab", "-framerate", str(fps_val), "-i", "desktop"])

    if clean_audio:
        cmd.extend(["-f", "dshow", "-i", f"audio={clean_audio}"])
    if need_even_pad:
        # Ensure window capture width & height are always even for NVENC / H.264 / H.265 / AV1
        cmd.extend(["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"])
    cmd.extend(
        [
            "-c:v",
            enc,
            "-b:v",
            f"{rate}M",
            "-maxrate",
            f"{int(rate * 1.5)}M",
            "-bufsize",
            f"{rate * 2}M",
        ]
    )
    if "nvenc" in enc:
        cmd.extend(["-preset", "p4", "-tune", "ll"])
    else:
        cmd.extend(["-preset", "veryfast", "-pix_fmt", "yuv420p"])
    if clean_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])
    cmd.append(str(output_file))
    return cmd


class RecorderController:
    """Manages manual, quick-play-linked, and auto-segmented recording across backends."""

    def __init__(
        self,
        *,
        watch_poll_sec: float = 1.0,
        split_settle_sec: float = 0.35,
        mid_offload_delay_sec: float = 2.0,
        critical_free_bytes: int = CRITICAL_FREE_BYTES,
        free_bytes_fn: Callable[[Path], int] | None = None,
        popen_fn: Callable[..., Any] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._offload_lock = threading.Lock()
        self._proc: Any = None
        self._active_file: str = ""
        self._active_files: list[str] = []
        self._active_backend: str = ""
        self._is_recording: bool = False
        self._session_started_ts: float = 0.0
        self._recording_started_mono: float = 0.0
        self._segment_started_mono: float = 0.0
        self._segment_seq: int = 0
        self._segments_completed: int = 0
        self._active_out_dir: str = ""
        self._used_fallback: bool = False
        self._last_guard_event: str = ""
        self._active_audio_device: str = ""
        self._active_capture_target: dict[str, Any] = {}
        self._active_policy: dict[str, Any] = resolve_effective_split_policy("off", 30, 2048)
        self._active_cfg: AppConfig | None = None
        self._active_paths: AppPaths | None = None
        self._active_system_env: dict[str, Any] | None = None
        self._active_win32_ops: Win32Ops | None = None
        self._active_ffmpeg_exe: str = ""
        self._active_ffmpeg_ext: str = "mkv"
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._watch_poll_sec = max(0.02, float(watch_poll_sec))
        self._split_settle_sec = max(0.01, float(split_settle_sec))
        self._mid_offload_delay_sec = max(0.01, float(mid_offload_delay_sec))
        self._critical_free_bytes = max(1, int(critical_free_bytes))
        self._free_bytes_fn = free_bytes_fn
        self._popen_fn = popen_fn

    @property
    def is_recording(self) -> bool:
        with self._lock:
            if self._proc is not None and self._proc.poll() is not None:
                self._proc = None
                self._is_recording = False
            return self._is_recording

    @property
    def active_file(self) -> str:
        with self._lock:
            return self._active_file

    def get_runtime_status(self) -> dict[str, Any]:
        with self._lock:
            active_now = self.is_recording
            now_m = time.monotonic()
            elapsed = round(max(0.0, now_m - self._recording_started_mono), 1) if active_now and self._recording_started_mono > 0 else 0.0
            seg_elapsed = round(max(0.0, now_m - self._segment_started_mono), 1) if active_now and self._segment_started_mono > 0 else 0.0
            cur_mb = 0.0
            if active_now and self._active_file:
                try:
                    p = Path(self._active_file)
                    if p.is_file():
                        cur_mb = round(p.stat().st_size / (1024**2), 2)
                except OSError:
                    pass
            return {
                "is_recording": active_now,
                "active_backend": self._active_backend if active_now else "",
                "active_file": self._active_file if active_now else "",
                "current_segment": self._segment_seq if active_now else 0,
                "segments_completed": self._segments_completed,
                "elapsed_sec": elapsed,
                "segment_elapsed_sec": seg_elapsed,
                "current_size_mb": cur_mb,
                "used_fallback": self._used_fallback if active_now else False,
                "active_out_dir": self._active_out_dir if active_now else "",
                "last_guard_event": self._last_guard_event,
                "split_policy": dict(self._active_policy),
                "capture_scope": str(self._active_capture_target.get("effective_scope") or "") if active_now else "",
                "capture_target_label": str(self._active_capture_target.get("label") or "") if active_now else "",
                "fell_back_to_desktop": bool(self._active_capture_target.get("fell_back_to_desktop", False)) if active_now else False,
            }

    def _effective_backend(self, cfg: AppConfig, system_env: dict[str, Any] | None = None) -> str:
        info = detect_recorders(cfg, system_env=system_env)
        return str(info["effective_backend"])

    def _effective_hotkeys(self, cfg: AppConfig, backend: str) -> tuple[str, str]:
        d_rec, d_rep = BACKEND_DEFAULT_HOTKEYS.get(backend, ("F9", "F10"))
        rec_hk = cfg.recorder.hotkey_record.strip() or d_rec
        rep_hk = cfg.recorder.hotkey_replay.strip() or d_rep
        return rec_hk, rep_hk

    def _finalize_ffmpeg_proc(self, proc: Any) -> None:
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if getattr(proc, "stdin", None):
                    proc.stdin.write(b"q\n")
                    proc.stdin.flush()
                proc.wait(timeout=6)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    def _spawn_ffmpeg_segment_locked(self, seq: int) -> Path:
        cfg = self._active_cfg
        assert cfg is not None
        out_dir = Path(self._active_out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = render_clip_filename(
            seq,
            cfg.recorder.filename_template,
            ext=self._active_ffmpeg_ext,
            preset=cfg.active_preset,
            backend="ffmpeg",
            mode=cfg.recorder.mode,
        )
        out_file = _unique_dest_path(out_dir, Path(fname))
        tgt = self._active_capture_target or {}
        cmd = build_ffmpeg_command(
            self._active_ffmpeg_exe,
            out_file,
            encoder=cfg.recorder.ffmpeg_encoder,
            container_fmt=self._active_ffmpeg_ext,
            bitrate_mbps=cfg.recorder.ffmpeg_bitrate_mbps,
            fps=cfg.recorder.ffmpeg_fps,
            system_env=self._active_system_env,
            audio_device=self._active_audio_device,
            capture_scope=str(tgt.get("effective_scope") or "desktop"),
            window_title=str(tgt.get("window_title") or ""),
            video_device=str(tgt.get("video_device") or ""),
        )
        creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
        popen_impl = self._popen_fn or subprocess.Popen
        self._proc = popen_impl(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._active_file = str(out_file)
        self._active_files.append(str(out_file))
        self._segment_seq = seq
        self._segment_started_mono = time.monotonic()
        return out_file

    def _watch_dirs_for_cfg(self, cfg: AppConfig) -> list[Path]:
        if cfg.recorder.source_watch_dir.strip():
            return [Path(cfg.recorder.source_watch_dir.strip())]
        return _default_watch_dirs()

    def _newest_active_file_bytes(self, dirs: list[Path], since_ts: float) -> int:
        newest_size = 0
        newest_mtime = 0.0
        for sdir in dirs:
            if not sdir.is_dir():
                continue
            try:
                for item in sdir.rglob("*"):
                    if not item.is_file() or item.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
                        continue
                    st = item.stat()
                    if st.st_mtime >= since_ts and st.st_mtime >= newest_mtime:
                        newest_mtime = st.st_mtime
                        newest_size = int(st.st_size)
            except OSError:
                continue
        return newest_size

    def _trigger_background_offload(self, cfg: AppConfig, backend: str, since_ts: float) -> None:
        if not cfg.recorder.auto_offload:
            return
        resolved = resolve_recording_dir(
            cfg.recorder.output_dir,
            cfg.recorder.fallback_dir,
            bitrate_mbps=cfg.recorder.ffmpeg_bitrate_mbps,
            min_free_gb=cfg.recorder.min_free_gb,
        )
        target_raw = str(resolved.get("path") or "").strip()
        if not target_raw or (resolved.get("configured") and not resolved.get("online")):
            return
        src_dirs = self._watch_dirs_for_cfg(cfg)
        delay = self._mid_offload_delay_sec

        def _worker() -> None:
            if not self._offload_lock.acquire(blocking=False):
                return
            try:
                time.sleep(delay)
                offload_clips_to_target(
                    src_dirs,
                    Path(target_raw),
                    since_ts=since_ts,
                    delete_source=True,
                    min_age_sec=max(0.05, delay * 0.5),
                    apply_naming=cfg.recorder.apply_naming_on_offload,
                    filename_template=cfg.recorder.filename_template,
                    preset=cfg.active_preset,
                    backend=backend,
                    mode=cfg.recorder.mode,
                )
            except Exception:
                pass
            finally:
                self._offload_lock.release()

        threading.Thread(target=_worker, name="WireDeck-MidOffload", daemon=True).start()

    def _get_free_bytes(self, path: Path) -> int:
        if self._free_bytes_fn is not None:
            return int(self._free_bytes_fn(path))
        return int(shutil.disk_usage(path).free)

    def _check_ffmpeg_storage_guard_locked(self) -> bool:
        """Check USB mount & free space for active FFmpeg recording. Return False if recording had to stop."""
        cfg = self._active_cfg
        ops = self._active_win32_ops or default_win32_ops()
        if cfg is None or not self._active_out_dir:
            return True

        out_dir = Path(self._active_out_dir)
        drive_missing = bool(out_dir.drive) and not Path(out_dir.drive + "\\").exists()
        if drive_missing or not out_dir.exists():
            # Primary USB disappeared mid-recording! Try fallback if different & online
            fb_raw = cfg.recorder.fallback_dir.strip()
            fb_ok = False
            if fb_raw and fb_raw != self._active_out_dir:
                fb_info = inspect_target_path(
                    fb_raw,
                    bitrate_mbps=cfg.recorder.ffmpeg_bitrate_mbps,
                    min_free_gb=cfg.recorder.min_free_gb,
                )
                fb_ok = bool(fb_info.get("online"))
            self._finalize_ffmpeg_proc(self._proc)
            self._segments_completed += 1
            if fb_ok:
                self._active_out_dir = fb_raw
                self._used_fallback = True
                next_seq = get_next_sequence_index(fb_raw)
                self._spawn_ffmpeg_segment_locked(next_seq)
                self._last_guard_event = "USB_FAILOVER"
                try:
                    ops.show_notification("WireDeck", "外接录像盘已断开，已自动切换至备用目录继续录制")
                except Exception:
                    pass
                return True
            self._proc = None
            self._is_recording = False
            self._active_file = ""
            self._last_guard_event = "USB_DISCONNECTED"
            try:
                ops.show_notification("WireDeck", "外接录像盘已断开，已安全停止录制")
            except Exception:
                pass
            return False

        try:
            free_b = self._get_free_bytes(out_dir)
        except OSError:
            return True

        min_free_bytes = int(max(0.5, float(cfg.recorder.min_free_gb or 5.0)) * (1024**3))
        if free_b < min_free_bytes and cfg.recorder.auto_prune:
            exclude = {Path(self._active_file)} if self._active_file else None
            prune_clips_by_quota(
                out_dir,
                max_storage_gb=cfg.recorder.max_storage_gb,
                min_free_gb=cfg.recorder.min_free_gb,
                free_bytes_fn=self._free_bytes_fn,
                exclude_paths=exclude,
            )
            try:
                free_b = self._get_free_bytes(out_dir)
            except OSError:
                pass

        if free_b < self._critical_free_bytes:
            self._finalize_ffmpeg_proc(self._proc)
            self._proc = None
            self._segments_completed += 1
            self._is_recording = False
            self._active_file = ""
            self._last_guard_event = "DISK_CRITICAL_STOP"
            try:
                ops.show_notification("WireDeck", "磁盘剩余空间不足，已安全封装保存当前录像并停止录制")
            except Exception:
                pass
            return False
        return True

    def _stop_monitor_thread(self) -> None:
        self._stop_event.set()
        th = self._monitor_thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=5.0)
        self._monitor_thread = None

    def _start_monitor_thread_locked(self) -> None:
        if self._active_backend != "ffmpeg" and self._active_policy.get("mode", "off") == "off":
            return
        self._stop_event.clear()
        th = threading.Thread(target=self._monitor_loop, name="WireDeck-SplitWatcher", daemon=True)
        self._monitor_thread = th
        th.start()

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self._watch_poll_sec):
            with self._lock:
                if not self._is_recording or self._active_cfg is None:
                    return
                if self._active_backend == "ffmpeg":
                    if not self._check_ffmpeg_storage_guard_locked():
                        return

                policy = self._active_policy
                time_sec = float(policy.get("time_sec") or 0)
                size_bytes = int(policy.get("size_bytes") or 0)
                seg_elapsed = time.monotonic() - self._segment_started_mono
                hit_time = time_sec > 0 and seg_elapsed >= time_sec

                hit_size = False
                if size_bytes > 0:
                    if self._active_backend == "ffmpeg" and self._active_file:
                        try:
                            p = Path(self._active_file)
                            if p.is_file() and p.stat().st_size >= size_bytes:
                                hit_size = True
                        except OSError:
                            pass
                    elif self._active_backend != "ffmpeg":
                        watch = self._watch_dirs_for_cfg(self._active_cfg)
                        sz = self._newest_active_file_bytes(watch, self._session_started_ts)
                        if sz >= size_bytes:
                            hit_size = True

                if not (hit_time or hit_size):
                    continue

                if self._active_backend == "ffmpeg":
                    old_proc = self._proc
                    self._finalize_ffmpeg_proc(old_proc)
                    self._segments_completed += 1
                    if self._stop_event.is_set():
                        self._proc = None
                        self._is_recording = False
                        return
                    next_seq = max(self._segment_seq + 1, get_next_sequence_index(self._active_out_dir))
                    self._spawn_ffmpeg_segment_locked(next_seq)
                    if self._active_cfg.recorder.auto_prune and self._active_out_dir:
                        prune_clips_by_quota(
                            Path(self._active_out_dir),
                            max_storage_gb=self._active_cfg.recorder.max_storage_gb,
                            min_free_gb=self._active_cfg.recorder.min_free_gb,
                            free_bytes_fn=self._free_bytes_fn,
                            exclude_paths={Path(self._active_file)} if self._active_file else None,
                        )
                    continue

                # Hotkey rollover: capture snapshot & wait settle outside lock
                ops = self._active_win32_ops or default_win32_ops()
                rec_hk, _ = self._effective_hotkeys(self._active_cfg, self._active_backend)
                cfg_snap = self._active_cfg
                bk_snap = self._active_backend
                sess_ts = self._session_started_ts
                if not rec_hk:
                    continue
                ops.send_hotkey(rec_hk)
                self._segments_completed += 1

            if self._stop_event.wait(self._split_settle_sec):
                return
            with self._lock:
                if not self._is_recording:
                    return
                ops.send_hotkey(rec_hk)
                self._segment_seq += 1
                self._segment_started_mono = time.monotonic()
            self._trigger_background_offload(cfg_snap, bk_snap, sess_ts)

    def split_now(
        self,
        cfg: AppConfig,
        paths: AppPaths,
        *,
        win32_ops: Win32Ops | None = None,
    ) -> ActionResult:
        """Immediately finalize current segment and roll over to `<next_seq>_...` seamlessly."""
        with self._lock:
            if not self.is_recording:
                return ActionResult(ok=False, code="NOT_RECORDING", message="当前未在录制中，无法切段", exit_code=1)
            ops = win32_ops or self._active_win32_ops or default_win32_ops()
            if self._active_backend == "ffmpeg":
                old_proc = self._proc
                self._finalize_ffmpeg_proc(old_proc)
                self._segments_completed += 1
                next_seq = max(self._segment_seq + 1, get_next_sequence_index(self._active_out_dir))
                out_file = self._spawn_ffmpeg_segment_locked(next_seq)
                return ActionResult(
                    ok=True,
                    code="SEGMENT_SPLIT",
                    message=f"已切入第 #{next_seq} 段：{out_file.name}",
                    details={"path": str(out_file), "seq": next_seq, "backend": "ffmpeg"},
                )

            rec_hk, _ = self._effective_hotkeys(cfg, self._active_backend)
            if not rec_hk:
                return ActionResult(ok=False, code="NO_RECORD_HOTKEY", message="未配置录制热键", exit_code=1)
            ops.send_hotkey(rec_hk)
            self._segments_completed += 1
            bk_snap = self._active_backend
            sess_ts = self._session_started_ts

        time.sleep(self._split_settle_sec)
        with self._lock:
            if not self._is_recording:
                return ActionResult(ok=False, code="STOPPED_DURING_SPLIT", message="录制已停止", exit_code=1)
            ops.send_hotkey(rec_hk)
            self._segment_seq += 1
            self._segment_started_mono = time.monotonic()
            next_seq = self._segment_seq
        self._trigger_background_offload(cfg, bk_snap, sess_ts)
        return ActionResult(
            ok=True,
            code="SEGMENT_SPLIT",
            message=f"已触发第 #{next_seq} 段切段滚转",
            details={"seq": next_seq, "backend": bk_snap},
        )

    def start_recording(
        self,
        cfg: AppConfig,
        paths: AppPaths,
        *,
        system_env: dict[str, Any] | None = None,
        win32_ops: Win32Ops | None = None,
    ) -> ActionResult:
        self._stop_monitor_thread()
        ops = win32_ops or default_win32_ops()
        backend = self._effective_backend(cfg, system_env=system_env)
        rec = cfg.recorder
        resolved = resolve_recording_dir(
            rec.output_dir,
            rec.fallback_dir,
            bitrate_mbps=rec.ffmpeg_bitrate_mbps,
            min_free_gb=rec.min_free_gb,
        )
        policy = resolve_effective_split_policy(
            rec.split_mode,
            rec.split_minutes,
            rec.split_size_mb,
            filesystem=str(resolved.get("filesystem") or ""),
        )
        ff_hint = _find_ffmpeg_exe(rec.ffmpeg_exe_path)
        video_devs = detect_ffmpeg_video_devices(ff_hint) if (rec.capture_scope == "capture_card" and ff_hint) else []
        capture_target = resolve_capture_target(
            cfg,
            video_devices=video_devs if rec.capture_scope == "capture_card" else None,
            win32_ops=ops,
        )
        if not capture_target.get("ready") or capture_target.get("effective_scope") == "blocked":
            return ActionResult(
                ok=False,
                code="CAPTURE_TARGET_MISSING",
                message=str(capture_target.get("warning") or "未检测到目标录屏画面"),
                exit_code=1,
            )

        if capture_target.get("hwnd") and capture_target.get("is_minimized"):
            ops.restore_window_no_activate(int(capture_target["hwnd"]))
            ops.sleep(0.15)

        with self._lock:
            self._active_cfg = cfg
            self._active_paths = paths
            self._active_system_env = system_env
            self._active_win32_ops = ops
            self._active_policy = policy
            self._active_capture_target = dict(capture_target)
            self._active_files = []
            self._segments_completed = 0
            self._last_guard_event = ""
            self._recording_started_mono = time.monotonic()
            self._segment_started_mono = self._recording_started_mono
            self._used_fallback = bool(resolved.get("used_fallback"))

            if backend == "ffmpeg":
                ffmpeg_exe = _find_ffmpeg_exe(rec.ffmpeg_exe_path)
                if not ffmpeg_exe:
                    return ActionResult(
                        ok=False,
                        code="FFMPEG_MISSING",
                        message="未找到 ffmpeg.exe，请在配置中指定路径或改用 OBS / NVIDIA / Game Bar",
                        exit_code=1,
                    )
                if resolved.get("configured") and not resolved.get("online"):
                    return ActionResult(
                        ok=False,
                        code="TARGET_OFFLINE",
                        message="; ".join(resolved.get("warnings") or ["目标录像目录离线"]),
                        exit_code=1,
                    )
                out_dir_raw = str(resolved.get("path") or (Path.home() / "Videos" / "WireDeck"))
                out_dir = Path(out_dir_raw)
                out_dir.mkdir(parents=True, exist_ok=True)
                ext = rec.ffmpeg_format if rec.ffmpeg_format in ("mkv", "mp4", "mov", "ts", "flv") else "mkv"

                raw_audio = rec.ffmpeg_audio_device.strip()
                if raw_audio.lower() == "auto":
                    self._active_audio_device = _pick_preferred_audio_device(detect_ffmpeg_audio_devices(ffmpeg_exe))
                else:
                    self._active_audio_device = raw_audio

                self._active_ffmpeg_exe = ffmpeg_exe
                self._active_ffmpeg_ext = ext
                self._active_out_dir = str(out_dir)
                seq = get_next_sequence_index(out_dir)
                out_file = self._spawn_ffmpeg_segment_locked(seq)
                self._active_backend = "ffmpeg"
                self._is_recording = True
                self._start_monitor_thread_locked()
                fb_note = "（主盘离线，已自动切换本地备用盘）" if self._used_fallback else ""
                tgt_note = f" [{capture_target.get('label')}]" if capture_target.get("label") else ""
                return ActionResult(
                    ok=True,
                    code="RECORDING_STARTED",
                    message=f"FFmpeg 正在录制{tgt_note}并直写：{out_file.name}{fb_note}",
                    details={
                        "path": str(out_file),
                        "backend": "ffmpeg",
                        "seq": seq,
                        "used_fallback": self._used_fallback,
                        "audio_device": self._active_audio_device,
                        "split_policy": policy,
                        "capture_target": capture_target,
                    },
                )

            self._active_out_dir = str(resolved.get("path") or "")
            self._segment_seq = get_next_sequence_index(self._active_out_dir) if self._active_out_dir else 1

            if backend == "obs":
                obs = detect_obs_info(rec.obs_exe_path)
                if not obs["installed"]:
                    return ActionResult(ok=False, code="OBS_MISSING", message="未检测到 obs64.exe，请检查 OBS 安装路径", exit_code=1)
                if not ops.is_process_running(Path(obs["exe_path"]).name):
                    args = ["--minimize-to-tray"]
                    if rec.mode == "replay":
                        args.append("--startreplaybuffer")
                    else:
                        args.append("--startrecording")
                    ops.launch_process(obs["exe_path"], args=args, cwd=str(Path(obs["exe_path"]).parent), minimized=True)
                    self._active_backend = "obs"
                    self._is_recording = True
                    return ActionResult(
                        ok=True,
                        code="OBS_STARTED",
                        message=f"已在后台启动 OBS（{'回放缓存' if rec.mode == 'replay' else '开始录制'}）",
                        details={"backend": "obs", "args": args, "capture_target": capture_target},
                    )
                rec_hk, _ = self._effective_hotkeys(cfg, "obs")
                ops.send_hotkey(rec_hk)
                self._active_backend = "obs"
                self._is_recording = True
                if rec.mode != "replay":
                    self._start_monitor_thread_locked()
                return ActionResult(ok=True, code="HOTKEY_SENT", message=f"已向运行中的 OBS 发送录制热键 {rec_hk}", details={"backend": "obs", "hotkey": rec_hk, "capture_target": capture_target})

            # NVIDIA / Game Bar / Custom rely on foreground window focus
            if capture_target.get("effective_scope") == "window" and capture_target.get("hwnd"):
                ops.focus_window(int(capture_target["hwnd"]))
                ops.sleep(0.2)

            if backend == "custom":
                cexe = rec.custom_exe_path.strip()
                if cexe and Path(cexe).is_file() and not ops.is_process_running(Path(cexe).name):
                    ops.launch_process(cexe, args=[], cwd=str(Path(cexe).parent), minimized=True)
                rec_hk, _ = self._effective_hotkeys(cfg, "custom")
                ops.send_hotkey(rec_hk)
                self._active_backend = "custom"
                self._is_recording = True
                if rec.mode != "replay":
                    self._start_monitor_thread_locked()
                return ActionResult(ok=True, code="HOTKEY_SENT", message=f"已启动/触发自定义录屏热键 {rec_hk}", details={"backend": "custom", "hotkey": rec_hk, "capture_target": capture_target})

            # NVIDIA / Game Bar
            rec_hk, _ = self._effective_hotkeys(cfg, backend)
            ops.send_hotkey(rec_hk)
            self._active_backend = backend
            self._is_recording = True
            if rec.mode != "replay":
                self._start_monitor_thread_locked()
            label = "NVIDIA App" if backend == "nvidia" else "Xbox Game Bar"
            return ActionResult(ok=True, code="HOTKEY_SENT", message=f"已触发 {label} 录制热键 {rec_hk}", details={"backend": backend, "hotkey": rec_hk, "capture_target": capture_target})

    def stop_recording(
        self,
        cfg: AppConfig,
        paths: AppPaths,
        *,
        win32_ops: Win32Ops | None = None,
    ) -> ActionResult:
        self._stop_monitor_thread()
        ops = win32_ops or default_win32_ops()
        with self._lock:
            backend = self._active_backend or self._effective_backend(cfg)

            if self._proc is not None and self._proc.poll() is None:
                self._finalize_ffmpeg_proc(self._proc)
                self._proc = None
                self._segments_completed += 1
                saved = self._active_file
                all_files = list(self._active_files)
                segs = self._segments_completed
                self._is_recording = False
                self._active_file = ""
                return ActionResult(
                    ok=True,
                    code="RECORDING_STOPPED",
                    message=f"已安全封装保存 {segs} 段录像（最新：{Path(saved).name if saved else ''}）",
                    details={"path": saved, "files": all_files, "segments_completed": segs},
                )

            rec_hk, _ = self._effective_hotkeys(cfg, backend)
            if rec_hk:
                ops.send_hotkey(rec_hk)
            self._is_recording = False
            self._active_file = ""
            return ActionResult(ok=True, code="RECORDING_STOPPED", message=f"已发送停止录制热键 {rec_hk}")

    def save_replay(
        self,
        cfg: AppConfig,
        *,
        system_env: dict[str, Any] | None = None,
        win32_ops: Win32Ops | None = None,
    ) -> ActionResult:
        ops = win32_ops or default_win32_ops()
        backend = self._active_backend or self._effective_backend(cfg, system_env=system_env)
        _, rep_hk = self._effective_hotkeys(cfg, backend)
        if not rep_hk:
            return ActionResult(ok=False, code="NO_REPLAY_HOTKEY", message="当前后端（FFmpeg 直录模式）不支持热键回放，请切换 OBS/NVIDIA/Game Bar", exit_code=1)
        ops.send_hotkey(rep_hk)
        return ActionResult(ok=True, code="REPLAY_SAVED", message=f"已触发保存高光回放热键 {rep_hk}", details={"hotkey": rep_hk})

    def on_session_start(
        self,
        cfg: AppConfig,
        paths: AppPaths,
        *,
        system_env: dict[str, Any] | None = None,
        win32_ops: Win32Ops | None = None,
    ) -> ActionResult | None:
        if not cfg.recorder.auto_link_quick_play or cfg.recorder.mode == "manual":
            return None
        self._session_started_ts = time.time() - 5.0
        return self.start_recording(cfg, paths, system_env=system_env, win32_ops=win32_ops)

    def on_session_end(
        self,
        cfg: AppConfig,
        paths: AppPaths,
        *,
        win32_ops: Win32Ops | None = None,
    ) -> list[ActionResult]:
        if not cfg.recorder.auto_link_quick_play:
            return []
        results: list[ActionResult] = []
        active_bk = self._active_backend or self._effective_backend(cfg)
        if self.is_recording:
            results.append(self.stop_recording(cfg, paths, win32_ops=win32_ops))

        resolved = resolve_recording_dir(
            cfg.recorder.output_dir,
            cfg.recorder.fallback_dir,
            bitrate_mbps=cfg.recorder.ffmpeg_bitrate_mbps,
            min_free_gb=cfg.recorder.min_free_gb,
        )
        target_raw = str(resolved.get("path") or "").strip()
        if resolved.get("configured") and not resolved.get("online"):
            target_raw = ""

        if target_raw and cfg.recorder.auto_offload:
            src_dirs = self._watch_dirs_for_cfg(cfg)
            results.append(
                offload_clips_to_target(
                    src_dirs,
                    Path(target_raw),
                    since_ts=self._session_started_ts,
                    delete_source=True,
                    apply_naming=cfg.recorder.apply_naming_on_offload,
                    filename_template=cfg.recorder.filename_template,
                    preset=cfg.active_preset,
                    backend=active_bk,
                    mode=cfg.recorder.mode,
                )
            )
        if target_raw and cfg.recorder.auto_prune:
            results.append(
                prune_clips_by_quota(
                    Path(target_raw),
                    max_storage_gb=cfg.recorder.max_storage_gb,
                    min_free_gb=cfg.recorder.min_free_gb,
                )
            )
        return results


def _capture_hwnd_png(hwnd: int, out_path: Path) -> bool:
    """Capture a single window's DWM surface cleanly (even when occluded by other windows)."""
    if os.name != "nt" or not hwnd:
        return False
    try:
        import ctypes
        from PIL import Image

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class _BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        rect = _RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = max(1, int(rect.right - rect.left))
        h = max(1, int(rect.bottom - rect.top))

        hdc_win = user32.GetWindowDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
        gdi32.SelectObject(hdc_mem, hbmp)
        user32.PrintWindow(hwnd, hdc_mem, 2)  # PW_RENDERFULLCONTENT = 2

        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, format="PNG")

        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)
        return out_path.is_file()
    except Exception:
        return False


def _capture_desktop_png(out_path: Path) -> bool:
    try:
        from PIL import ImageGrab

        img = ImageGrab.grab()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, format="PNG")
        return out_path.is_file()
    except Exception:
        return False


def capture_screenshot(
    cfg: AppConfig,
    *,
    output_dir: str = "",
    win32_ops: Win32Ops | None = None,
    capture_hwnd_fn: Callable[[int, Path], bool] | None = None,
    capture_desktop_fn: Callable[[Path], bool] | None = None,
) -> ActionResult:
    """Take an instant PNG screenshot honoring `capture_scope` (window-isolated when locked)."""
    ops = win32_ops or default_win32_ops()
    rec = cfg.recorder
    base_dir = (output_dir or rec.output_dir).strip()
    resolved = resolve_recording_dir(
        base_dir,
        rec.fallback_dir,
        bitrate_mbps=rec.ffmpeg_bitrate_mbps,
        min_free_gb=rec.min_free_gb,
    )
    if resolved.get("configured") and not resolved.get("online"):
        return ActionResult(
            ok=False,
            code="TARGET_OFFLINE",
            message="; ".join(resolved.get("warnings") or ["目标存储盘离线"]),
            exit_code=1,
        )

    shot_dir = Path(str(resolved.get("path") or (Path.home() / "Videos" / "WireDeck"))) / "Screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)

    target = resolve_capture_target(cfg, win32_ops=ops)
    eff = str(target.get("effective_scope") or "desktop")
    if eff == "blocked" or not target.get("ready"):
        return ActionResult(
            ok=False,
            code="CAPTURE_TARGET_MISSING",
            message=str(target.get("warning") or "未找到可截取的游戏画面"),
            exit_code=1,
        )

    hwnd = int(target.get("hwnd") or 0)
    if hwnd and target.get("is_minimized"):
        ops.restore_window_no_activate(hwnd)
        ops.sleep(0.15)

    seq = get_next_sequence_index(shot_dir, extensions=(".png", ".jpg", ".jpeg"))
    fname = render_clip_filename(
        seq,
        rec.filename_template,
        ext="png",
        preset=cfg.active_preset,
        backend="screenshot",
        mode=rec.mode,
    )
    out_file = _unique_dest_path(shot_dir, Path(fname))

    hwnd_fn = capture_hwnd_fn or _capture_hwnd_png
    desk_fn = capture_desktop_fn or _capture_desktop_png
    ok = hwnd_fn(hwnd, out_file) if (eff == "window" and hwnd) else desk_fn(out_file)
    if not ok:
        return ActionResult(ok=False, code="SCREENSHOT_FAILED", message="截取画面失败，请检查目标窗口是否正在渲染", exit_code=1)

    return ActionResult(
        ok=True,
        code="SCREENSHOT_SAVED",
        message=f"已保存纯净截图：{out_file.name}",
        details={
            "path": str(out_file),
            "filename": out_file.name,
            "seq": seq,
            "scope": eff,
            "target": str(target.get("label") or ""),
        },
    )
