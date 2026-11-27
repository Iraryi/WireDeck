"""Interactive uninstaller discovery & opt-in cleanup engine (all optional items default OFF)."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from wiredeck.config import AppConfig
from wiredeck.models import ActionResult
from wiredeck.paths import AppPaths, ensure_within
from wiredeck.presets import ALL_PRESET_IDS


@dataclass(frozen=True)
class AssociatedAppSpec:
    id: str
    display_name: str
    category: str
    match_tokens: tuple[str, ...]
    exact_only: bool = False


ASSOCIATED_APP_SPECS: tuple[AssociatedAppSpec, ...] = (
    AssociatedAppSpec("xstreaming", "XStreaming", "stream_client", ("xstreaming",)),
    AssociatedAppSpec("chiaki", "Chiaki-ng / Chiaki", "stream_client", ("chiaki",)),
    AssociatedAppSpec("moonlight", "Moonlight Game Streaming", "stream_client", ("moonlight",)),
    AssociatedAppSpec("sunshine", "Sunshine", "stream_client", ("sunshine",)),
    AssociatedAppSpec(
        "ps_remote",
        "PS Remote Play",
        "stream_client",
        ("ps remote play", "playstation remote play"),
    ),
    AssociatedAppSpec("obs", "OBS Studio", "recorder", ("obs studio",)),
    AssociatedAppSpec("magpie", "Magpie", "enhancer", ("magpie",)),
    AssociatedAppSpec("lossless_scaling", "Lossless Scaling", "enhancer", ("lossless scaling",)),
    AssociatedAppSpec("ffmpeg", "FFmpeg", "recorder", ("ffmpeg",), exact_only=True),
)

# Never match OS components, drivers, runtimes, Xbox Game Bar, or WireDeck itself
EXCLUDED_SYSTEM_TOKENS: tuple[str, ...] = (
    "wiredeck",
    "nvidia",
    "geforce",
    "amd ",
    "radeon",
    "intel(",
    "microsoft",
    "windows",
    "xbox game bar",
    "gaming services",
    "webview2",
    "visual c++",
    "directx",
    ".net",
)


def read_windows_uninstall_registry() -> list[dict[str, Any]]:
    """Read installed software entries from HKCU/HKLM Uninstall registry hives."""
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    hives = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    entries: list[dict[str, Any]] = []
    for root_hive, subkey_path in hives:
        try:
            with winreg.OpenKey(root_hive, subkey_path) as parent:
                count = winreg.QueryInfoKey(parent)[0]
                for idx in range(count):
                    try:
                        child_name = winreg.EnumKey(parent, idx)
                        with winreg.OpenKey(parent, child_name) as child:
                            def _val(name: str) -> str:
                                try:
                                    v, _ = winreg.QueryValueEx(child, name)
                                    return str(v).strip()
                                except OSError:
                                    return ""

                            disp = _val("DisplayName")
                            uninst = _val("QuietUninstallString") or _val("UninstallString")
                            std_uninst = _val("UninstallString") or uninst
                            if not disp or not std_uninst:
                                continue
                            entries.append(
                                {
                                    "key": child_name,
                                    "display_name": disp,
                                    "display_version": _val("DisplayVersion"),
                                    "publisher": _val("Publisher"),
                                    "install_location": _val("InstallLocation"),
                                    "uninstall_string": std_uninst,
                                    "quiet_uninstall_string": _val("QuietUninstallString"),
                                }
                            )
                    except OSError:
                        continue
        except OSError:
            continue
    return entries


def _is_excluded_entry(display_name: str) -> bool:
    low = (display_name or "").strip().lower()
    if not low or low == "steam":
        return True
    return any(tok in low for tok in EXCLUDED_SYSTEM_TOKENS)


def _used_by_wiredeck(spec_id: str, cfg: AppConfig) -> bool:
    if spec_id == "obs" and (cfg.recorder.backend in ("obs", "auto") and bool(cfg.recorder.obs_exe_path.strip())):
        return True
    if spec_id == "ffmpeg" and (cfg.recorder.backend == "ffmpeg" or bool(cfg.recorder.ffmpeg_exe_path.strip())):
        return True
    for pid in ALL_PRESET_IDS:
        ps = cfg.presets.get(pid)
        if not ps:
            continue
        custom = ps.custom_client_path.lower()
        if spec_id in custom:
            return True
    if spec_id in ("xstreaming",) and cfg.active_preset == "xbox":
        return True
    if spec_id in ("chiaki", "ps_remote") and cfg.active_preset == "playstation":
        return True
    if spec_id in ("moonlight", "sunshine") and cfg.active_preset in ("handheld", "other"):
        return True
    return False


def scan_associated_apps(
    cfg: AppConfig,
    *,
    registry_entries: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Detect companion apps installed on the system; EVERY item defaults to UNCHECKED."""
    raw = registry_entries if registry_entries is not None else read_windows_uninstall_registry()
    seen_specs: set[str] = set()
    found: list[dict[str, Any]] = []

    for entry in raw:
        disp = str(entry.get("display_name") or "").strip()
        uninstall_cmd = str(entry.get("uninstall_string") or "").strip()
        if not disp or not uninstall_cmd or _is_excluded_entry(disp):
            continue
        low = disp.lower()
        for spec in ASSOCIATED_APP_SPECS:
            if spec.id in seen_specs:
                continue
            matched = (
                low in spec.match_tokens
                if spec.exact_only
                else any(tok in low for tok in spec.match_tokens)
            )
            if not matched:
                continue
            seen_specs.add(spec.id)
            found.append(
                {
                    "id": f"assoc_{spec.id}",
                    "spec_id": spec.id,
                    "name": disp,
                    "category": spec.category,
                    "version": str(entry.get("display_version") or ""),
                    "publisher": str(entry.get("publisher") or ""),
                    "install_location": str(entry.get("install_location") or ""),
                    "uninstall_command": uninstall_cmd,
                    "used_by_wiredeck": _used_by_wiredeck(spec.id, cfg),
                    "default_selected": False,
                }
            )
            break

    return found


def find_wiredeck_desktop_shortcuts(desktop_dir: Path | None = None) -> list[Path]:
    """Return Desktop `.lnk` shortcuts strictly verified to point to WireDeck.exe."""
    candidates: list[Path] = []
    search_dirs: list[Path] = []
    if desktop_dir is not None:
        search_dirs.append(Path(desktop_dir))
    else:
        search_dirs.append(Path.home() / "Desktop")
        userprof = os.environ.get("USERPROFILE", "").strip()
        if userprof:
            search_dirs.append(Path(userprof) / "Desktop")

    seen_dirs: set[Path] = set()
    for d in search_dirs:
        rd = d.resolve(strict=False)
        if rd in seen_dirs or not rd.is_dir():
            continue
        seen_dirs.add(rd)
        for lnk in rd.glob("*.lnk"):
            try:
                blob = lnk.read_bytes()
            except OSError:
                continue
            if (b"WireDeck.exe" in blob) or ("WireDeck.exe".encode("utf-16le") in blob):
                candidates.append(lnk)
    return candidates


def _dir_stats(path: Path) -> tuple[int, float]:
    if path.is_file():
        try:
            return 1, round(path.stat().st_size / (1024 * 1024), 2)
        except OSError:
            return 1, 0.0
    if not path.is_dir():
        return 0, 0.0
    count = 0
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for fn in files:
            fp = Path(root) / fn
            try:
                if not fp.is_symlink():
                    count += 1
                    total += fp.stat().st_size
            except OSError:
                continue
    return count, round(total / (1024 * 1024), 2)


def _detect_nat_active_quick() -> bool:
    if os.name != "nt":
        return False
    try:
        from wiredeck.ps_runner import run_powershell
        from wiredeck.version import NAT_NAME

        res = run_powershell(f"(Get-NetNat -Name '{NAT_NAME}' -ErrorAction SilentlyContinue) -ne $null", timeout=3)
        return res.returncode == 0 and res.stdout.strip().lower() == "true"
    except Exception:
        return False


def discover_uninstall_items(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    nat_active: bool | None = None,
    wifi_tuned: bool | None = None,
    registry_entries: list[dict[str, Any]] | None = None,
    desktop_dir: Path | None = None,
) -> dict[str, Any]:
    """Discover removable WireDeck data items + installed companion apps (ALL default UNCHECKED)."""
    is_nat = _detect_nat_active_quick() if nat_active is None else bool(nat_active)
    is_wifi = (paths.runtime_dir / "wifi_backup.json").is_file() if wifi_tuned is None else bool(wifi_tuned)

    shortcuts = find_wiredeck_desktop_shortcuts(desktop_dir)
    plg_cnt, plg_mb = _dir_stats(paths.plugins_dir)
    ext_cnt, ext_mb = _dir_stats(paths.extensions_dir)

    data_files = 0
    data_mb = 0.0
    for p in (paths.config_file, paths.plugin_configs_dir, paths.logs_dir, paths.exports_dir, paths.runtime_dir):
        c, m = _dir_stats(p)
        data_files += c
        data_mb = round(data_mb + m, 2)

    gpu_backup = (paths.runtime_dir / "gpu_prefs_backup.json").is_file()

    wiredeck_items: list[dict[str, Any]] = []
    if shortcuts:
        wiredeck_items.append(
            {
                "id": "wd_shortcuts",
                "count": len(shortcuts),
                "size_mb": 0.0,
                "paths": [str(s) for s in shortcuts],
                "default_selected": False,
            }
        )
    if (plg_cnt + ext_cnt) > 0:
        wiredeck_items.append(
            {
                "id": "wd_plugins",
                "count": plg_cnt + ext_cnt,
                "size_mb": round(plg_mb + ext_mb, 2),
                "paths": [str(paths.plugins_dir), str(paths.extensions_dir)],
                "default_selected": False,
            }
        )
    if data_files > 0:
        wiredeck_items.append(
            {
                "id": "wd_userdata",
                "count": data_files,
                "size_mb": data_mb,
                "paths": [str(paths.data_dir)],
                "default_selected": False,
            }
        )
    if gpu_backup:
        wiredeck_items.append(
            {
                "id": "wd_gpu_prefs",
                "count": 1,
                "size_mb": 0.0,
                "paths": [str(paths.runtime_dir / "gpu_prefs_backup.json")],
                "default_selected": False,
            }
        )

    associated = scan_associated_apps(cfg, registry_entries=registry_entries)
    uninstaller_exe = paths.exe_dir / "unins000.exe"

    return {
        "ok": True,
        "is_portable": paths.is_portable,
        "exe_dir": str(paths.exe_dir),
        "data_dir": str(paths.data_dir),
        "uninstaller_exe": str(uninstaller_exe) if uninstaller_exe.is_file() else "",
        "network_restore": {
            "needed": bool(is_nat or is_wifi),
            "nat_active": is_nat,
            "wifi_tuned": is_wifi,
            "default_selected": False,
        },
        "wiredeck_items": wiredeck_items,
        "associated_apps": associated,
        "associated_cleanup_default_enabled": False,
        "recorded_clips_protected": True,
        "recorded_clips_dir": cfg.recorder.output_dir.strip(),
    }


def parse_uninstall_command(cmd: str) -> tuple[str, list[str] | str]:
    """Parse an official Windows UninstallString cleanly without shell expansion."""
    raw = (cmd or "").strip()
    if not raw:
        raise ValueError("Empty uninstall command")
    if raw.lower().startswith("steam://uninstall/"):
        return "uri", raw
    parts = shlex.split(raw, posix=False)
    cleaned = [p[1:-1] if len(p) >= 2 and p[0] == '"' and p[-1] == '"' else p for p in parts]
    if not cleaned:
        raise ValueError(f"Unable to parse uninstall command: {raw}")
    return "argv", cleaned


def _default_launch_associated_uninstall(app_entry: dict[str, Any]) -> ActionResult:
    name = str(app_entry.get("name") or app_entry.get("id") or "")
    cmd = str(app_entry.get("uninstall_command") or "").strip()
    try:
        kind, parsed = parse_uninstall_command(cmd)
        if kind == "uri":
            if os.name == "nt" and hasattr(os, "startfile"):
                os.startfile(str(parsed))  # type: ignore[arg-type]
                return ActionResult(ok=True, code="UNINSTALLER_URI_OPENED", message=f"已调用 Steam 卸载：{name}")
            return ActionResult(ok=False, code="UNSUPPORTED_PLATFORM", message=f"无法在此平台启动 {parsed}", exit_code=1)
        proc = subprocess.run(list(parsed), shell=False, timeout=300)
        return ActionResult(
            ok=proc.returncode in (0, 3010),
            code="UNINSTALLER_FINISHED" if proc.returncode in (0, 3010) else "UNINSTALLER_EXIT_NONZERO",
            message=f"{name} 官方卸载程序已退出（code={proc.returncode}）",
            details={"returncode": proc.returncode},
            exit_code=0 if proc.returncode in (0, 3010) else 1,
        )
    except Exception as exc:
        return ActionResult(ok=False, code="UNINSTALLER_FAILED", message=f"调用 {name} 官方卸载程序失败：{exc}", exit_code=1)


def _restore_gpu_prefs(paths: AppPaths) -> ActionResult:
    backup = paths.runtime_dir / "gpu_prefs_backup.json"
    if not backup.is_file():
        return ActionResult(ok=True, code="NO_GPU_BACKUP", message="无 GPU 首选项备份需要还原")
    if os.name == "nt":
        try:
            import winreg

            data = json.loads(backup.read_text(encoding="utf-8-sig"))
            key_path = r"Software\Microsoft\DirectX\UserGpuPreferences"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                for exe_path, prev in (data if isinstance(data, dict) else {}).items():
                    if prev is None:
                        try:
                            winreg.DeleteValue(key, str(exe_path))
                        except OSError:
                            pass
                    else:
                        winreg.SetValueEx(key, str(exe_path), 0, winreg.REG_SZ, str(prev))
        except Exception as exc:
            return ActionResult(ok=False, code="GPU_RESTORE_FAILED", message=f"还原 GPU 首选项失败：{exc}", exit_code=1)
    backup.unlink(missing_ok=True)
    return ActionResult(ok=True, code="GPU_PREFS_RESTORED", message="已还原 WireDeck 修改的显卡首选项")


def execute_uninstall_cleanup(
    paths: AppPaths,
    cfg: AppConfig,
    selected_ids: list[str],
    *,
    restore_network: bool = False,
    discovered: dict[str, Any] | None = None,
    desktop_dir: Path | None = None,
    restore_network_fn: Callable[[], ActionResult] | None = None,
    launch_uninstall_fn: Callable[[dict[str, Any]], ActionResult] | None = None,
    restore_gpu_prefs_fn: Callable[[AppPaths], ActionResult] | None = None,
) -> ActionResult:
    """Execute strictly user-selected cleanup items; empty selection touches nothing."""
    chosen: list[str] = []
    for raw_id in selected_ids or []:
        sid = str(raw_id).strip()
        if sid and sid not in chosen:
            chosen.append(sid)

    if not chosen and not restore_network:
        return ActionResult(
            ok=True,
            code="NO_EXTRA_CLEANUP",
            message="仅卸载 WireDeck 主程序，已保留所有个人配置、插件、录像与关联程序",
            details={"executed": [], "failed": []},
        )

    executed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    if restore_network and restore_network_fn is not None:
        net_res = restore_network_fn()
        entry = {"id": "network_restore", "ok": net_res.ok, "message": net_res.message}
        (executed if net_res.ok else failed).append(entry)

    assoc_map: dict[str, dict[str, Any]] = {}
    if any(x.startswith("assoc_") for x in chosen):
        disc = discovered if discovered is not None else discover_uninstall_items(paths, cfg, desktop_dir=desktop_dir)
        for app_entry in disc.get("associated_apps") or []:
            assoc_map[str(app_entry.get("id") or "")] = app_entry

    gpu_fn = restore_gpu_prefs_fn or _restore_gpu_prefs
    uninst_fn = launch_uninstall_fn or _default_launch_associated_uninstall

    # Order cleanup safely: shortcuts, gpu prefs, plugins, userdata LAST (so config stays until end)
    ordered = [x for x in ("wd_shortcuts", "wd_gpu_prefs", "wd_plugins") if x in chosen]
    ordered += [x for x in chosen if x.startswith("assoc_")]
    if "wd_userdata" in chosen:
        ordered.append("wd_userdata")
    unknown = [x for x in chosen if x not in ordered]
    for u in unknown:
        failed.append({"id": u, "ok": False, "message": f"未知清理项：{u}"})

    for item_id in ordered:
        if item_id == "wd_shortcuts":
            removed = 0
            for lnk in find_wiredeck_desktop_shortcuts(desktop_dir):
                try:
                    lnk.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
            executed.append({"id": item_id, "ok": True, "message": f"已移除 {removed} 个 WireDeck 桌面快捷方式"})

        elif item_id == "wd_gpu_prefs":
            gres = gpu_fn(paths)
            (executed if gres.ok else failed).append({"id": item_id, "ok": gres.ok, "message": gres.message})

        elif item_id == "wd_plugins":
            for target in (paths.plugins_dir, paths.extensions_dir):
                safe_t = ensure_within(paths.data_dir, target)
                if safe_t.exists():
                    shutil.rmtree(safe_t, ignore_errors=True)
            executed.append({"id": item_id, "ok": True, "message": "已清理 WireDeck 插件与 AI 扩展目录"})

        elif item_id == "wd_userdata":
            for sub in (
                paths.config_file,
                paths.plugin_configs_dir,
                paths.logs_dir,
                paths.exports_dir,
                paths.runtime_dir,
            ):
                safe_s = ensure_within(paths.data_dir, sub)
                if safe_s.is_file():
                    safe_s.unlink(missing_ok=True)
                elif safe_s.is_dir():
                    shutil.rmtree(safe_s, ignore_errors=True)
            # Remove data_dir itself only if completely empty (never deletes user clips placed nearby)
            try:
                if paths.data_dir.is_dir() and not any(paths.data_dir.iterdir()):
                    paths.data_dir.rmdir()
            except OSError:
                pass
            executed.append({"id": item_id, "ok": True, "message": "已清理 WireDeck 配置、日志与诊断导出缓存"})

        elif item_id.startswith("assoc_"):
            app_entry = assoc_map.get(item_id)
            if not app_entry:
                failed.append({"id": item_id, "ok": False, "message": f"未在受支持的关联程序清单中找到：{item_id}"})
                continue
            ares = uninst_fn(app_entry)
            (executed if ares.ok else failed).append({"id": item_id, "ok": ares.ok, "message": ares.message})

    ok = len(failed) == 0
    return ActionResult(
        ok=ok,
        code="UNINSTALL_CLEANUP_DONE" if ok else "UNINSTALL_CLEANUP_PARTIAL",
        message=f"已完成 {len(executed)} 项卸载清理" + (f"，{len(failed)} 项未完成" if failed else ""),
        details={"executed": executed, "failed": failed},
        exit_code=0 if ok else 1,
    )


def launch_self_uninstall(
    paths: AppPaths,
    *,
    pid: int | None = None,
    popen_fn: Callable[..., Any] = subprocess.Popen,
) -> ActionResult:
    """Launch Inno Setup silent binary removal (or portable binary cleanup) after interactive wizard confirms."""
    unins_exe = paths.exe_dir / "unins000.exe"
    if unins_exe.is_file():
        cmd = [str(unins_exe), "/SILENT", "/NORESTART"]
        try:
            popen_fn(cmd, cwd=str(paths.exe_dir))
        except Exception as exc:
            return ActionResult(ok=False, code="SELF_UNINSTALL_FAILED", message=f"启动卸载器失败：{exc}", exit_code=1)
        return ActionResult(
            ok=True,
            code="SELF_UNINSTALL_LAUNCHED",
            message="正在移除 WireDeck 程序文件…",
            details={"mode": "installer", "should_exit": True, "command": cmd},
        )

    if paths.is_portable:
        token = uuid.uuid4().hex[:8]
        ps_script = Path(tempfile.gettempdir()) / f"wiredeck_portable_remove_{token}.ps1"
        exe_dir_ps = str(paths.exe_dir).replace("'", "''")
        data_empty_check = (
            f"$dataDir = Join-Path '{exe_dir_ps}' 'data'\n"
            "if ((Test-Path -LiteralPath $dataDir) -and ((Get-ChildItem -LiteralPath $dataDir -Force | Measure-Object).Count -eq 0)) {\n"
            "  Remove-Item -LiteralPath $dataDir -Force\n"
            "}\n"
        )
        script = (
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            f"$pidToWait = {int(pid if pid is not None else os.getpid())}\n"
            "for ($i = 0; $i -lt 40; $i++) {\n"
            "  if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) { break }\n"
            "  Start-Sleep -Milliseconds 300\n"
            "}\n"
            f"foreach ($name in @('WireDeck.exe','wiredeck-cli.exe','_internal','portable.mode','assets')) {{\n"
            f"  $p = Join-Path '{exe_dir_ps}' $name\n"
            "  if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }\n"
            "}\n"
            f"{data_empty_check}"
            "Remove-Item -LiteralPath $PSCommandPath -Force\n"
        )
        ps_script.write_text(script, encoding="utf-8-sig", newline="\n")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_script)]
        try:
            popen_fn(cmd, creationflags=creation_flags, close_fds=True)
        except Exception as exc:
            return ActionResult(ok=False, code="PORTABLE_UNINSTALL_FAILED", message=f"启动便携版清理失败：{exc}", exit_code=1)
        return ActionResult(
            ok=True,
            code="SELF_UNINSTALL_LAUNCHED",
            message="正在清理便携版程序文件…",
            details={"mode": "portable", "should_exit": True, "script_path": str(ps_script), "command": cmd},
        )

    return ActionResult(
        ok=True,
        code="DEV_MODE_CLEANUP_ONLY",
        message="开发运行目录不包含安装版卸载器，已完成所选清理项",
        details={"mode": "dev", "should_exit": False},
    )
