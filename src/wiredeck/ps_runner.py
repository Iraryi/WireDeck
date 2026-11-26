"""UTF-8 PowerShell subprocess runner, admin check, and standard Windows UAC worker."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wiredeck.models import ActionResult

CREATE_NO_WINDOW = 0x08000000
ERROR_CANCELLED = 1223
SEE_MASK_NOCLOSEPROCESS = 0x00000040
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
SW_HIDE = 0


@dataclass
class PsResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def is_admin() -> bool:
    """Return True when the current process holds Windows Administrator privileges."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _encode_ps_script(script: str) -> str:
    preamble = (
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
    )
    full_script = preamble + script
    return base64.b64encode(full_script.encode("utf-16le")).decode("ascii")


def run_powershell(
    script: str,
    timeout: int = 15,
    extra_env: dict[str, str] | None = None,
) -> PsResult:
    """Execute a PowerShell script silently with strict UTF-8 encoding."""
    encoded = _encode_ps_script(script)
    cmd = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=creationflags,
        )
        return PsResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return PsResult(
            returncode=-1,
            stdout=out,
            stderr=err or f"PowerShell execution timed out after {timeout}s",
            timed_out=True,
        )
    except FileNotFoundError as exc:
        return PsResult(returncode=127, stdout="", stderr=str(exc), timed_out=False)


def run_ps_file(
    script_path: Path,
    timeout: int = 10,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> PsResult:
    """Execute an existing .ps1 script file silently with guaranteed UTF-8 parsing (even without BOM)."""
    resolved = script_path.resolve()
    try:
        script_body = resolved.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        script_body = resolved.read_text(encoding="gb18030", errors="replace")
    except OSError as exc:
        return PsResult(returncode=1, stdout="", stderr=str(exc), timed_out=False)

    encoded = _encode_ps_script(script_body)
    cmd = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            cwd=str(cwd or resolved.parent),
            creationflags=creationflags,
        )
        return PsResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return PsResult(
            returncode=-1,
            stdout=out,
            stderr=err or f"Script timed out after {timeout}s",
            timed_out=True,
        )
    except FileNotFoundError as exc:
        return PsResult(returncode=127, stdout="", stderr=str(exc), timed_out=False)


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def run_elevated_worker(
    cli_exe: Path | None,
    req_path: Path,
    out_path: Path,
    timeout: int = 60,
    python_fallback: Path | None = None,
) -> ActionResult:
    """Invoke standard Windows UAC 'runas' worker process and parse its JSON response."""
    if os.name != "nt":
        return ActionResult(
            ok=False,
            code="UNSUPPORTED_OS",
            message="仅支持在 Windows 上执行提权操作",
            exit_code=1,
        )

    if cli_exe is not None and cli_exe.is_file():
        file_to_run = str(cli_exe.resolve())
        params = subprocess.list2cmdline(
            ["--worker", str(req_path.resolve()), "--out", str(out_path.resolve())]
        )
        work_dir = str(cli_exe.resolve().parent)
    else:
        py_exe = str((python_fallback or Path(sys.executable)).resolve())
        file_to_run = py_exe
        params = subprocess.list2cmdline(
            [
                "-m",
                "wiredeck.cli",
                "--worker",
                str(req_path.resolve()),
                "--out",
                str(out_path.resolve()),
            ]
        )
        work_dir = str(Path(__file__).resolve().parents[1])

    sei = _SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = file_to_run
    sei.lpParameters = params
    sei.lpDirectory = work_dir
    sei.nShow = SW_HIDE

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    ok = shell32.ShellExecuteExW(ctypes.byref(sei))
    if not ok:
        err_code = ctypes.get_last_error()
        if err_code == ERROR_CANCELLED:
            return ActionResult(
                ok=False,
                code="UAC_CANCELLED",
                message="已取消管理员授权，未做任何修改",
                exit_code=3,
            )
        return ActionResult(
            ok=False,
            code="UAC_LAUNCH_FAILED",
            message=f"无法拉起提权进程（错误码 {err_code}）",
            exit_code=3,
            details={"win32_error": err_code},
        )

    try:
        if sei.hProcess:
            wait_res = kernel32.WaitForSingleObject(sei.hProcess, int(timeout * 1000))
            if wait_res == WAIT_TIMEOUT:
                kernel32.TerminateProcess(sei.hProcess, 1)
                return ActionResult(
                    ok=False,
                    code="WORKER_TIMEOUT",
                    message=f"提权操作超时（>{timeout}s）",
                    exit_code=1,
                )
    finally:
        if sei.hProcess:
            kernel32.CloseHandle(sei.hProcess)

    if not out_path.is_file():
        return ActionResult(
            ok=False,
            code="WORKER_NO_OUTPUT",
            message="提权进程已退出但未返回结果",
            exit_code=1,
        )

    try:
        data: dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8-sig"))
        return ActionResult(
            ok=bool(data.get("ok", False)),
            code=str(data.get("code", "UNKNOWN")),
            message=str(data.get("message", "")),
            exit_code=int(data.get("exit_code", 0 if data.get("ok") else 1)),
            details=dict(data.get("details") or {}),
            log_file=data.get("log_file"),
        )
    except Exception as exc:
        return ActionResult(
            ok=False,
            code="WORKER_BAD_OUTPUT",
            message=f"提权结果解析失败: {exc}",
            exit_code=1,
        )
