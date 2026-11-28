"""Native Windows window, hotkey, process, and silent notification helpers."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from wiredeck.ps_runner import CREATE_NO_WINDOW, run_powershell
from wiredeck.version import APP_VERSION

_KEYEVENTF_KEYUP = 0x0002
_STARTF_USESHOWWINDOW = 0x00000001
_SW_SHOWNOACTIVATE = 4
_SW_SHOWMINNOACTIVE = 7
_SW_RESTORE = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

_MODIFIER_VK: dict[str, int] = {
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "menu": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "lwin": 0x5B,
    "super": 0x5B,
}

_SPECIAL_VK: dict[str, int] = {
    "enter": 0x0D,
    "return": 0x0D,
    "space": 0x20,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "printscreen": 0x2C,
    "pause": 0x13,
}

_EXE_BASENAME_RE = re.compile(r"^[A-Za-z0-9._ -]+\.exe$", re.IGNORECASE)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def parse_hotkey(hotkey: str) -> list[int]:
    """Parse hotkey string like 'Ctrl+Alt+S' or 'Win+Shift+A' into Windows VK codes."""
    raw = (hotkey or "").strip()
    if not raw:
        raise ValueError("Empty hotkey string")

    tokens = [tok.strip() for tok in raw.split("+") if tok.strip()]
    if not tokens:
        raise ValueError(f"Invalid hotkey: {hotkey!r}")

    vks: list[int] = []
    for tok in tokens:
        low = tok.lower()
        if low in _MODIFIER_VK:
            vks.append(_MODIFIER_VK[low])
        elif low in _SPECIAL_VK:
            vks.append(_SPECIAL_VK[low])
        elif len(tok) == 1 and "a" <= low <= "z":
            vks.append(ord(low.upper()))
        elif len(tok) == 1 and "0" <= low <= "9":
            vks.append(ord(low))
        elif low.startswith("f") and low[1:].isdigit():
            fn = int(low[1:])
            if 1 <= fn <= 24:
                vks.append(0x70 + fn - 1)
            else:
                raise ValueError(f"Unsupported function key: {tok!r}")
        else:
            raise ValueError(f"Unsupported hotkey token: {tok!r}")
    return vks


def send_hotkey(hotkey: str) -> bool:
    """Send a system-wide key combination cleanly using Win32 keybd_event."""
    vks = parse_hotkey(hotkey)
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    modifiers = {0x10, 0x11, 0x12, 0x5B}
    pressed_mods: list[int] = []
    primary_keys: list[int] = []
    for vk in vks:
        if vk in modifiers:
            pressed_mods.append(vk)
        else:
            primary_keys.append(vk)
    if not primary_keys and pressed_mods:
        primary_keys = [pressed_mods.pop()]

    try:
        for mod in pressed_mods:
            user32.keybd_event(mod, 0, 0, 0)
            time.sleep(0.02)
        for key in primary_keys:
            user32.keybd_event(key, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(key, 0, _KEYEVENTF_KEYUP, 0)
        return True
    finally:
        for mod in reversed(pressed_mods):
            try:
                user32.keybd_event(mod, 0, _KEYEVENTF_KEYUP, 0)
            except Exception:
                pass


def show_silent_notification(title: str, message: str) -> bool:
    """Show a non-blocking Windows tray balloon tip without opening WireDeck GUI."""
    if os.name != "nt":
        return False
    safe_title = (title or "WireDeck").strip()[:64]
    safe_msg = (message or "").strip()[:240]
    if not safe_msg:
        return False
    ps = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = {_ps_quote(safe_title)}
$n.BalloonTipText = {_ps_quote(safe_msg)}
$n.Visible = $true
$n.ShowBalloonTip(3500)
Start-Sleep -Milliseconds 3600
$n.Dispose()
"""
    res = run_powershell(ps, timeout=8)
    return res.returncode == 0


def _query_process_image_name(pid: int) -> str:
    if os.name != "nt" or pid <= 0:
        return ""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(512)
        buf = ctypes.create_unicode_buffer(512)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_window_by_pid_or_name(
    pid: int | None = None,
    process_name: str = "",
    title_contains: str = "",
) -> int:
    """Locate a visible top-level window HWND matching PID, process name, or title."""
    if os.name != "nt":
        return 0

    user32 = ctypes.windll.user32
    target_proc = Path(process_name).name.lower() if process_name.strip() else ""
    target_title = title_contains.strip().lower()
    matched_hwnd = 0

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd: int, _lparam: int) -> bool:
        nonlocal matched_hwnd
        if not user32.IsWindowVisible(hwnd):
            return True

        win_pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
        if pid and int(win_pid.value) != int(pid):
            return True

        if target_proc:
            img_name = _query_process_image_name(int(win_pid.value))
            if img_name != target_proc:
                return True

        length = user32.GetWindowTextLengthW(hwnd)
        if target_title:
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if target_title not in buf.value.lower():
                return True
        elif length <= 0 and not pid:
            return True

        matched_hwnd = int(hwnd)
        return False

    cb = WNDENUMPROC(_callback)
    user32.EnumWindows(cb, 0)
    return matched_hwnd


def wait_for_window(
    pid: int | None = None,
    process_name: str = "",
    title_contains: str = "",
    timeout_sec: float = 15.0,
    poll_interval: float = 0.25,
) -> int:
    """Wait until a visible top-level window appears or return 0 on timeout."""
    deadline = time.monotonic() + max(0.2, float(timeout_sec))
    interval = max(0.05, float(poll_interval))
    while True:
        hwnd = find_window_by_pid_or_name(
            pid=pid,
            process_name=process_name,
            title_contains=title_contains,
        )
        if hwnd:
            return hwnd
        if time.monotonic() >= deadline:
            return 0
        time.sleep(interval)


def focus_window(hwnd: int) -> bool:
    """Restore if minimized and bring a window to foreground reliably."""
    if os.name != "nt" or not hwnd:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, _SW_RESTORE)
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur_tid = kernel32.GetCurrentThreadId()
        attached = False
        if fg_tid and cur_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(fg_tid, cur_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            ok = bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(fg_tid, cur_tid, False)
        return ok
    except Exception:
        return False


_EXCLUDED_WINDOW_CLASSES: frozenset[str] = frozenset(
    {
        "progman",
        "workerw",
        "shell_traywnd",
        "shell_secondarytraywnd",
        "windows.ui.core.corewindow",
        "applicationframewindow",
    }
)


def list_open_windows(*, exclude_processes: set[str] | None = None) -> list[dict[str, Any]]:
    """Enumerate visible user desktop windows with process, class, size, and minimized state."""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    excluded = {"wiredeck.exe", *{str(p).strip().lower() for p in (exclude_processes or set())}}
    results: list[dict[str, Any]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        t_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, t_buf, length + 1)
        title = t_buf.value.strip()
        if not title:
            return True

        c_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, c_buf, 256)
        cls_name = c_buf.value.strip()
        if cls_name.lower() in _EXCLUDED_WINDOW_CLASSES:
            return True

        win_pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
        proc_name = _query_process_image_name(int(win_pid.value))
        if proc_name in excluded:
            return True

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = max(0, int(rect.right - rect.left))
        height = max(0, int(rect.bottom - rect.top))
        minimized = bool(user32.IsIconic(hwnd))
        if not minimized and (width < 80 or height < 50):
            return True

        results.append(
            {
                "hwnd": int(hwnd),
                "pid": int(win_pid.value),
                "title": title,
                "process_name": proc_name,
                "class_name": cls_name,
                "is_minimized": minimized,
                "width": width,
                "height": height,
            }
        )
        return True

    cb = WNDENUMPROC(_callback)
    user32.EnumWindows(cb, 0)
    return results


def restore_window_no_activate(hwnd: int) -> bool:
    """Restore a minimized window without stealing foreground focus so capture frames resume."""
    if os.name != "nt" or not hwnd:
        return False
    user32 = ctypes.windll.user32
    try:
        if user32.IsIconic(hwnd):
            return bool(user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE))
        return True
    except Exception:
        return False


def is_process_running(exe_name: str) -> bool:
    """Return True if a Windows process matching exact exe basename is currently running."""
    base = Path(exe_name).name
    if os.name != "nt" or not _EXE_BASENAME_RE.match(base):
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {base}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        return base.lower() in proc.stdout.lower()
    except Exception:
        return False


def is_pid_alive(pid: int) -> bool:
    """Check whether a Windows PID is still active."""
    if os.name != "nt" or pid <= 0:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def wait_for_pid_exit(pid: int, poll_interval: float = 1.0, max_wait_sec: float = 0.0) -> bool:
    """Wait until PID exits (returns True on exit, False on timeout if max_wait_sec > 0)."""
    if pid <= 0:
        return True
    deadline = (time.monotonic() + max_wait_sec) if max_wait_sec > 0 else None
    interval = max(0.1, float(poll_interval))
    while is_pid_alive(pid):
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


def launch_detached_process(
    exe: str,
    args: list[str] | None = None,
    cwd: str = "",
    minimized: bool = False,
) -> int:
    """Launch an external Windows executable cleanly without showing a console."""
    exe_path = str(exe).strip()
    if not exe_path:
        raise ValueError("Missing executable path")
    cmd = [exe_path, *(args or [])]
    work_dir = cwd.strip() or (str(Path(exe_path).parent) if Path(exe_path).parent.exists() else None)

    kwargs: dict[str, object] = {}
    if work_dir:
        kwargs["cwd"] = work_dir
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        if minimized:
            si.dwFlags |= _STARTF_USESHOWWINDOW
            si.wShowWindow = _SW_SHOWMINNOACTIVE
        kwargs["startupinfo"] = si
    proc = subprocess.Popen(cmd, **kwargs)  # type: ignore[arg-type]
    return int(getattr(proc, "pid", 0) or 0)


def stop_process_by_name(exe_name: str) -> bool:
    """Terminate processes matching exact .exe basename safely without wildcards."""
    base = Path(exe_name).name
    if os.name != "nt" or not _EXE_BASENAME_RE.match(base):
        return False
    try:
        proc = subprocess.run(
            ["taskkill", "/IM", base, "/T"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except Exception:
        return False


def restart_detached_process(
    exe: str,
    args: list[str] | None = None,
    cwd: str = "",
    minimized: bool = False,
) -> int:
    """Stop existing instances of `exe` cleanly and relaunch it."""
    exe_path = str(exe).strip()
    if not exe_path:
        raise ValueError("Missing executable path")
    base = Path(exe_path).name
    if base:
        stop_process_by_name(base)
        time.sleep(0.6)
    return launch_detached_process(exe_path, args=args, cwd=cwd, minimized=minimized)


def set_keep_awake(enable: bool) -> bool:
    """Keep display & system awake via SetThreadExecutionState while streaming."""
    if os.name != "nt":
        return False
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    flags = (ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED) if enable else ES_CONTINUOUS
    try:
        res = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        return bool(res)
    except Exception:
        return False


_GPU_PREF_REG_PATH = r"Software\Microsoft\DirectX\UserGpuPreferences"


def get_gpu_preference(exe_path: str) -> str:
    """Read Windows per-app GPU preference from HKCU UserGpuPreferences (no UAC needed)."""
    target = str(exe_path or "").strip()
    if os.name != "nt" or not target:
        return "default"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GPU_PREF_REG_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, target)
            s = str(val)
            if "GpuPreference=2" in s:
                return "high_performance"
            if "GpuPreference=1" in s:
                return "power_saving"
    except Exception:
        pass
    return "default"


def _backup_gpu_pref_once(backup_file: Path, exe_path: str, old_mode: str) -> None:
    try:
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, str] = {}
        if backup_file.is_file():
            raw = json.loads(backup_file.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                data = {str(k): str(v) for k, v in raw.items()}
        if exe_path not in data:
            data[exe_path] = old_mode
            backup_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def set_gpu_preference(
    exe_path: str,
    mode: str,
    *,
    backup_file: Path | None = None,
) -> bool:
    """Bind or unbind a Windows .exe to High Performance dGPU (GpuPreference=2;) in HKCU."""
    target = str(exe_path or "").strip()
    norm_mode = (mode or "default").strip().lower()
    if os.name != "nt" or not target:
        return False
    try:
        import winreg

        if backup_file is not None:
            _backup_gpu_pref_once(backup_file, target, get_gpu_preference(target))

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _GPU_PREF_REG_PATH, 0, winreg.KEY_SET_VALUE | winreg.KEY_READ) as key:
            if norm_mode == "high_performance":
                winreg.SetValueEx(key, target, 0, winreg.REG_SZ, "GpuPreference=2;")
            elif norm_mode == "power_saving":
                winreg.SetValueEx(key, target, 0, winreg.REG_SZ, "GpuPreference=1;")
            else:
                try:
                    winreg.DeleteValue(key, target)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


ALLOWED_DOWNLOAD_HOSTS: frozenset[str] = frozenset(
    {
        "download.nvidia.com",
        "cn.download.nvidia.com",
        "us.download.nvidia.com",
        "uk.download.nvidia.com",
        "de.download.nvidia.com",
        "jp.download.nvidia.com",
        "github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)


def _is_allowed_download_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if h in ALLOWED_DOWNLOAD_HOSTS:
        return True
    return h.endswith(".download.nvidia.com") or h.endswith(".githubusercontent.com")


def download_vendor_file(
    url: str,
    filename: str = "",
    *,
    sha256: str = "",
    dest_dir: Path | None = None,
    open_after: bool = False,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Download an official signed installer/binary from an allowlisted HTTPS domain."""
    parsed = urllib.parse.urlparse((url or "").strip())
    if parsed.scheme.lower() != "https" or not _is_allowed_download_host(parsed.hostname or ""):
        raise ValueError(f"Blocked non-allowlisted download URL: {url}")

    raw_name = filename.strip() or Path(parsed.path).name or "download.bin"
    safe_name = Path(re.sub(r'[<>:"/\\|?*]+', "_", raw_name)).name
    target_dir = Path(dest_dir) if dest_dir is not None else (Path.home() / "Downloads")
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / safe_name
    part = dest.with_suffix(dest.suffix + ".part")

    expected = (sha256 or "").strip().lower()
    if expected.startswith("sha256:"):
        expected = expected.split(":", 1)[1].strip()
    if expected and len(expected) != 64:
        raise ValueError("Invalid SHA-256 checksum string")

    req = urllib.request.Request(url.strip(), headers={"User-Agent": f"WireDeck/{APP_VERSION}"})
    hasher = hashlib.sha256()
    total_bytes = 0
    try:
        with urlopen_fn(req, timeout=45.0) as resp:
            with part.open("wb") as fh:
                while True:
                    chunk = resp.read(512 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    hasher.update(chunk)
                    total_bytes += len(chunk)
        digest = hasher.hexdigest().lower()
        if expected and digest != expected:
            part.unlink(missing_ok=True)
            raise ValueError(f"SHA-256 mismatch: expected {expected}, got {digest}")
        os.replace(part, dest)
    except Exception:
        part.unlink(missing_ok=True)
        raise

    if open_after and os.name == "nt":
        try:
            os.startfile(str(dest))  # type: ignore[attr-defined]
        except Exception:
            pass

    return {"ok": True, "path": str(dest), "size_bytes": total_bytes, "sha256": digest}


@dataclass
class Win32Ops:
    """Injectable bundle of Win32 operations for deterministic unit testing."""

    show_notification: Callable[[str, str], bool] = show_silent_notification
    send_hotkey: Callable[[str], bool] = send_hotkey
    find_window: Callable[..., int] = find_window_by_pid_or_name
    wait_window: Callable[..., int] = wait_for_window
    focus_window: Callable[[int], bool] = focus_window
    list_windows: Callable[..., list[dict[str, Any]]] = list_open_windows
    restore_window_no_activate: Callable[[int], bool] = restore_window_no_activate
    is_process_running: Callable[[str], bool] = is_process_running
    wait_for_pid_exit: Callable[..., bool] = wait_for_pid_exit
    launch_process: Callable[..., int] = launch_detached_process
    restart_process: Callable[..., int] = restart_detached_process
    stop_process: Callable[[str], bool] = stop_process_by_name
    set_keep_awake: Callable[[bool], bool] = set_keep_awake
    set_gpu_preference: Callable[..., bool] = set_gpu_preference
    download_file: Callable[..., dict[str, Any]] = download_vendor_file
    sleep: Callable[[float], None] = time.sleep


def default_win32_ops() -> Win32Ops:
    """Return the live Windows implementation bundle."""
    return Win32Ops()
