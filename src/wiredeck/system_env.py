"""Lightweight hardware, display refresh rate, power, and GPU driver probe with short TTL cache."""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

from wiredeck.ps_runner import CREATE_NO_WINDOW, PsResult, run_powershell

_VIRTUAL_GPU_RE = re.compile(
    r"(gameviewer|parsec|virtual\s*display|microsoft\s*basic|indirect\s*display|spacedesk|deskscreen|oray)",
    re.IGNORECASE,
)

_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SEC = 10.0
_CACHED_TS = 0.0
_CACHED_ENV: dict[str, Any] | None = None


def normalize_nvidia_driver_version(wddm_ver: str) -> str:
    """Convert WDDM version like `32.0.15.7284` to NVIDIA version `572.84`."""
    raw = (wddm_ver or "").strip()
    if not raw:
        return ""
    if re.match(r"^\d{3}\.\d{2}$", raw):
        return raw
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) >= 5:
        tail = digits[-5:]
        major = tail[:3]
        minor = tail[3:]
        if int(major) >= 300:
            return f"{major}.{minor}"
    return raw


def supports_nvidia_smooth_motion(gpu_name: str) -> bool:
    """Return True if the NVIDIA GPU officially supports driver-level Smooth Motion (RTX 40/50 series)."""
    name = (gpu_name or "").upper()
    if "RTX" not in name:
        return False
    return bool(re.search(r"RTX\s*(40\d{2}|50\d{2})", name))


def _query_nvidia_smi_versions() -> dict[str, str]:
    """Query exact NVIDIA driver versions per GPU via `nvidia-smi` if installed."""
    if os.name != "nt":
        return {}
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            creationflags=CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            return {}
        out: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) >= 2:
                out[parts[0].lower()] = parts[1]
        return out
    except Exception:
        return {}


def _is_nvidia_app_installed() -> bool:
    if os.name != "nt":
        return False
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates = [
        Path(pf) / "NVIDIA Corporation" / "NVIDIA app" / "CEF" / "NVIDIA app.exe",
        Path(pf) / "NVIDIA Corporation" / "NVIDIA App" / "CEF" / "NVIDIA app.exe",
    ]
    return any(p.is_file() for p in candidates)


def collect_system_env(
    *,
    runner: Callable[[str, int], PsResult] = run_powershell,
) -> dict[str, Any]:
    """Collect GPU, NVIDIA driver, display refresh rate, and battery status safely."""
    ps = r"""
$gpus = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | ForEach-Object {
    [PSCustomObject]@{
        name = [string]$_.Name
        vendor = [string]$_.AdapterCompatibility
        driver_version = [string]$_.DriverVersion
        refresh_hz = [int]($_.CurrentRefreshRate | ForEach-Object { if ($_ -gt 0) { $_ } else { 0 } })
        width = [int]($_.CurrentHorizontalResolution | ForEach-Object { if ($_ -gt 0) { $_ } else { 0 } })
        height = [int]($_.CurrentVerticalResolution | ForEach-Object { if ($_ -gt 0) { $_ } else { 0 } })
    }
})
$batt = @(Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -ExpandProperty BatteryStatus)
$btPad = @(Get-PnpDevice -Class Bluetooth,HIDClass -Status OK -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -match 'Xbox|DualSense|DualShock|Controller|Gamepad|Pro Controller|Switch' } | Select-Object -ExpandProperty FriendlyName)
@{
    gpus = $gpus
    battery_statuses = $batt
    bt_gamepads = $btPad
} | ConvertTo-Json -Depth 4 -Compress
"""
    raw_gpus: list[dict[str, Any]] = []
    battery_statuses: list[int] = []
    bt_gamepads: list[str] = []

    res = runner(ps, 10)
    if res.returncode == 0 and res.stdout.strip():
        try:
            parsed = json.loads(res.stdout.strip())
            if isinstance(parsed, dict):
                g = parsed.get("gpus")
                if isinstance(g, dict):
                    raw_gpus = [g]
                elif isinstance(g, list):
                    raw_gpus = [x for x in g if isinstance(x, dict)]
                bs = parsed.get("battery_statuses")
                if isinstance(bs, int):
                    battery_statuses = [bs]
                elif isinstance(bs, list):
                    battery_statuses = [int(x) for x in bs if isinstance(x, (int, float))]
                btp = parsed.get("bt_gamepads")
                if isinstance(btp, str) and btp.strip():
                    bt_gamepads = [btp.strip()]
                elif isinstance(btp, list):
                    bt_gamepads = [str(x).strip() for x in btp if str(x).strip()]
        except Exception:
            pass

    smi_map = _query_nvidia_smi_versions()
    gpus: list[dict[str, Any]] = []
    virtual_displays: list[str] = []
    max_refresh = 0
    primary_res = ""

    for item in raw_gpus:
        name = str(item.get("name") or "").strip()
        vendor = str(item.get("vendor") or "").strip()
        if not name:
            continue
        if _VIRTUAL_GPU_RE.search(name) or _VIRTUAL_GPU_RE.search(vendor):
            virtual_displays.append(name)
            continue

        hz = int(item.get("refresh_hz") or 0)
        w = int(item.get("width") or 0)
        h = int(item.get("height") or 0)
        res_str = f"{w}x{h}" if w > 0 and h > 0 else ""
        if hz > max_refresh:
            max_refresh = hz
        if res_str and not primary_res:
            primary_res = res_str

        is_nv = ("nvidia" in name.lower()) or ("nvidia" in vendor.lower())
        raw_ver = str(item.get("driver_version") or "").strip()
        if is_nv:
            smi_ver = smi_map.get(name.lower()) or (next(iter(smi_map.values()), "") if len(smi_map) == 1 else "")
            drv_ver = smi_ver or normalize_nvidia_driver_version(raw_ver)
        else:
            drv_ver = raw_ver

        is_igpu = bool(
            re.search(r"(uhd\s*graphics|iris|radeon\s*\(tm\)\s*graphics|arc\s*graphics)", name, re.IGNORECASE)
            and not is_nv
        )
        is_discrete = is_nv or (not is_igpu and ("radeon rx" in name.lower() or "arc a" in name.lower() or "arc b" in name.lower()))
        is_laptop = "laptop" in name.lower() or "mobile" in name.lower() or bool(battery_statuses)

        gpus.append(
            {
                "name": name,
                "vendor": vendor,
                "driver_version": drv_ver,
                "is_nvidia": is_nv,
                "is_discrete": is_discrete,
                "is_laptop": is_laptop,
                "supports_smooth_motion": supports_nvidia_smooth_motion(name) if is_nv else False,
                "refresh_hz": hz,
                "resolution": res_str,
            }
        )

    primary_nv = next((g for g in gpus if g["is_nvidia"]), None)
    has_dgpu = any(g["is_discrete"] for g in gpus)
    has_igpu = any(not g["is_discrete"] for g in gpus)

    controllers_info = detect_controllers(bt_names=bt_gamepads)
    return {
        "gpus": gpus,
        "primary_nvidia_gpu": primary_nv,
        "is_dual_gpu": bool(has_dgpu and has_igpu),
        "max_refresh_hz": max_refresh if max_refresh > 0 else 60,
        "primary_resolution": primary_res or "1920x1080",
        "virtual_displays": virtual_displays,
        "on_battery": any(s == 1 for s in battery_statuses),
        "nvidia_app_installed": _is_nvidia_app_installed(),
        "controllers": controllers_info,
    }


def _probe_xinput_controllers() -> list[dict[str, Any]]:
    """Query connected XInput controller slots 0..3 and their battery levels via Win32 ctypes."""
    if os.name != "nt":
        return []
    dll = None
    for name in ("xinput1_4.dll", "xinput9_1_0.dll"):
        try:
            dll = ctypes.WinDLL(name)
            break
        except OSError:
            continue
    if dll is None:
        return []

    class _XINPUT_GAMEPAD(ctypes.Structure):
        _fields_ = [
            ("wButtons", wintypes.WORD),
            ("bLeftTrigger", ctypes.c_ubyte),
            ("bRightTrigger", ctypes.c_ubyte),
            ("sThumbLX", ctypes.c_short),
            ("sThumbLY", ctypes.c_short),
            ("sThumbRX", ctypes.c_short),
            ("sThumbRY", ctypes.c_short),
        ]

    class _XINPUT_STATE(ctypes.Structure):
        _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", _XINPUT_GAMEPAD)]

    class _XINPUT_BATTERY_INFORMATION(ctypes.Structure):
        _fields_ = [("BatteryType", ctypes.c_ubyte), ("BatteryLevel", ctypes.c_ubyte)]

    get_state = dll.XInputGetState
    get_state.argtypes = [wintypes.DWORD, ctypes.POINTER(_XINPUT_STATE)]
    get_state.restype = wintypes.DWORD
    get_batt = getattr(dll, "XInputGetBatteryInformation", None)
    if get_batt is not None:
        get_batt.argtypes = [wintypes.DWORD, ctypes.c_ubyte, ctypes.POINTER(_XINPUT_BATTERY_INFORMATION)]
        get_batt.restype = wintypes.DWORD

    level_map = {0: "empty", 1: "low", 2: "medium", 3: "full"}
    found: list[dict[str, Any]] = []
    for slot in range(4):
        st = _XINPUT_STATE()
        if get_state(slot, ctypes.byref(st)) != 0:
            continue
        conn = "wired"
        blevel = "wired"
        if get_batt is not None:
            binfo = _XINPUT_BATTERY_INFORMATION()
            if get_batt(slot, 0, ctypes.byref(binfo)) == 0:
                if binfo.BatteryType == 1:
                    conn = "wired"
                    blevel = "wired"
                elif binfo.BatteryType in (2, 3):
                    conn = "wireless"
                    blevel = level_map.get(int(binfo.BatteryLevel), "medium")
                elif binfo.BatteryType == 0:
                    conn = "unknown"
                    blevel = "unknown"
        found.append({"slot": slot, "connection": conn, "battery_level": blevel})
    return found


def detect_controllers(
    *,
    xinput_probe: Callable[[], list[dict[str, Any]]] | None = None,
    bt_names: list[str] | None = None,
) -> dict[str, Any]:
    """Summarize XInput controller count, battery warnings, and Bluetooth gamepad coexistence."""
    probe_fn = xinput_probe or _probe_xinput_controllers
    try:
        ctrls = list(probe_fn() or [])
    except Exception:
        ctrls = []
    bt_list = [str(x).strip() for x in (bt_names or []) if str(x).strip()]
    low_slots = [
        int(c.get("slot", 0))
        for c in ctrls
        if str(c.get("connection") or "") == "wireless" and str(c.get("battery_level") or "") in ("empty", "low")
    ]
    has_bt = bool(bt_list) or any(str(c.get("connection") or "") == "wireless" for c in ctrls)
    return {
        "count": len(ctrls),
        "controllers": ctrls,
        "bluetooth_names": bt_list,
        "has_bluetooth_gamepad": has_bt,
        "low_battery_slots": low_slots,
    }


def collect_system_env_cached(*, force: bool = False) -> dict[str, Any]:
    """Return short-TTL cached system hardware & environment snapshot."""
    global _CACHED_TS, _CACHED_ENV
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force and _CACHED_ENV is not None and (now - _CACHED_TS) < _CACHE_TTL_SEC:
            return dict(_CACHED_ENV)
    env = collect_system_env()
    with _CACHE_LOCK:
        _CACHED_ENV = env
        _CACHED_TS = now
    return dict(env)


def evaluate_system_health(
    *,
    system_env: dict[str, Any],
    cfg_dict: dict[str, Any],
    recorder_detected: dict[str, Any],
    target_status: dict[str, Any],
    effective_split: dict[str, Any],
    fallback_pending_count: int = 0,
    installed_plugins: list[dict[str, Any]] | None = None,
    wifi_band_24g: bool = False,
) -> dict[str, Any]:
    """Produce a compact whole-system readiness & health summary across power, GPU, storage, and plugins."""
    warnings: list[dict[str, str]] = []
    on_battery = bool(system_env.get("on_battery"))
    if on_battery:
        warnings.append(
            {
                "code": "ON_BATTERY",
                "title": "当前处于电池供电",
                "detail": "建议接入笔记本电源后再高刷串流或录屏，避免 GPU 降频导致掉帧或热点限流。",
            }
        )

    rec_cfg = cfg_dict.get("recorder") if isinstance(cfg_dict.get("recorder"), dict) else {}
    out_dir = str(rec_cfg.get("output_dir") or "").strip()
    auto_link = bool(rec_cfg.get("auto_link_quick_play"))
    if (out_dir or auto_link) and not target_status.get("online"):
        warnings.append(
            {
                "code": "REC_TARGET_OFFLINE",
                "title": "录屏外接存储未在线",
                "detail": "; ".join(target_status.get("warnings") or ["尚未设置目标目录或外接 U盘未插入"]),
            }
        )
    elif target_status.get("online"):
        for w in target_status.get("warnings") or []:
            if "剩余空间" in w or "low" in w.lower():
                warnings.append({"code": "REC_LOW_SPACE", "title": "录像磁盘余量偏低", "detail": str(w)})
                break

    eff_bk = str(recorder_detected.get("effective_backend") or "obs")
    obs_info = recorder_detected.get("obs") if isinstance(recorder_detected.get("obs"), dict) else {}
    obs_rec_path = str(obs_info.get("record_path") or "").strip()
    if eff_bk == "obs" and obs_info.get("installed") and out_dir and target_status.get("online"):
        if obs_rec_path.rstrip("\\/").lower() != out_dir.rstrip("\\/").lower():
            warnings.append(
                {
                    "code": "OBS_PATH_UNSYNCED",
                    "title": "OBS 输出目录尚未同步",
                    "detail": f"OBS 当前输出到 {obs_rec_path or '-'}，与 WireDeck 设定 {out_dir} 不一致，可点击「同步目录与分段到 OBS」。",
                }
            )

    if int(fallback_pending_count or 0) > 0 and target_status.get("online"):
        warnings.append(
            {
                "code": "FALLBACK_PENDING",
                "title": f"备用目录有 {int(fallback_pending_count)} 个暂存录像待迁回",
                "detail": "外接主盘已重新挂载，可在「录屏存储 → 存储与归档」一键迁回主盘。",
            }
        )

    nv = system_env.get("primary_nvidia_gpu") or {}
    max_hz = int(system_env.get("max_refresh_hz") or 60)
    gpu_txt = f"{nv.get('name')} ({nv.get('driver_version')})" if nv else "集成/通用显卡"
    active_plg = [p for p in (installed_plugins or []) if p.get("valid") and p.get("enabled")]

    ctrl_info = system_env.get("controllers") if isinstance(system_env.get("controllers"), dict) else {}
    low_slots = [int(x) for x in (ctrl_info.get("low_battery_slots") or [])]
    has_bt_pad = bool(ctrl_info.get("has_bluetooth_gamepad"))
    ctrl_cnt = int(ctrl_info.get("count") or 0)
    if low_slots:
        warnings.append(
            {
                "code": "CONTROLLER_LOW_BATTERY",
                "title": f"无线手柄电量偏低（槽位 {', '.join('#' + str(s + 1) for s in low_slots)}）",
                "detail": "建议立即给手柄插线充电，避免串流对战中途断连。",
            }
        )
    if has_bt_pad and wifi_band_24g:
        warnings.append(
            {
                "code": "BT_24G_COEXISTENCE",
                "title": "蓝牙手柄与 2.4 GHz Wi-Fi 共存干扰",
                "detail": "同频干扰可能导致手感和画面波动；建议改用有线连接手柄或切回 5 GHz 热点。",
            }
        )

    if ctrl_cnt > 0:
        first = (ctrl_info.get("controllers") or [{}])[0]
        ctrl_val = f"已连接 {ctrl_cnt} 个 ({'有线' if first.get('connection') == 'wired' else '无线/' + str(first.get('battery_level', ''))})"
    elif ctrl_info.get("bluetooth_names"):
        ctrl_val = f"蓝牙手柄在线：{ctrl_info['bluetooth_names'][0]}"
    else:
        ctrl_val = "暂未检测到 XInput 手柄"

    items = [
        {"id": "power", "label": "供电状态", "value": "电池供电 (建议插电)" if on_battery else "已接交流电源", "ok": not on_battery},
        {"id": "gpu", "label": "显卡与屏幕", "value": f"{gpu_txt} · {system_env.get('primary_resolution', '1920x1080')} @ {max_hz}Hz", "ok": True},
        {"id": "controller", "label": "游戏手柄", "value": ctrl_val, "ok": not low_slots and not (has_bt_pad and wifi_band_24g)},
        {
            "id": "recorder",
            "label": "录屏与分段",
            "value": f"{eff_bk.upper()} · {effective_split.get('mode', 'off')} · {target_status.get('free_gb', 0)} GB 可用",
            "ok": not any(w["code"] in ("REC_TARGET_OFFLINE", "REC_LOW_SPACE", "OBS_PATH_UNSYNCED") for w in warnings),
        },
        {"id": "plugins", "label": "联动扩展插件", "value": f"{len(active_plg)} 个已启用", "ok": True},
    ]
    return {
        "ok": True,
        "overall": "warn" if warnings else "ok",
        "warning_count": len(warnings),
        "warnings": warnings,
        "items": items,
    }
