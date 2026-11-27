"""Headless CUI entrypoint (`wiredeck-cli.exe`) and elevated UAC worker."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from wiredeck.actions import SUPPORTED_ACTIONS, execute_action
from wiredeck.config import AppConfig, load_config
from wiredeck.diagnostics import classify_snapshot, run_diagnosis
from wiredeck.exporter import export_diagnostic_bundle
from wiredeck.extensions import (
    discover_extensions,
    inspect_extensions,
    run_extension,
    validate_extension_dir,
)
from wiredeck.models import ActionResult, Severity
from wiredeck.paths import (
    AppPaths,
    get_cli_executable,
    resolve_app_paths,
    resolve_app_paths_from_data_dir,
)
from wiredeck.plugins import (
    fetch_plugin_catalog,
    install_plugin_from_github,
    list_installed_plugins,
    set_plugin_enabled,
    uninstall_plugin,
)
from wiredeck.recorder import (
    detect_recorders,
    inspect_target_path,
    list_recorded_clips,
    list_storage_drives,
    offload_clips_to_target,
    preview_clip_filenames,
    prune_clips_by_quota,
    resolve_effective_split_policy,
)
from wiredeck.presets import ALL_PRESET_IDS, PRESET_ALIASES, normalize_preset_id
from wiredeck.probes import collect_system_snapshot
from wiredeck.ps_runner import is_admin, run_elevated_worker, run_powershell
from wiredeck.system_env import collect_system_env, evaluate_system_health
from wiredeck.toolkits import discover_all_toolkits, discover_toolkits, run_toolkit
from wiredeck.version import APP_NAME, APP_VERSION, NAT_NAME, SCHEMA_VERSION


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _emit(payload: dict[str, Any], as_json: bool, text_fallback: str = "") -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text_fallback or json.dumps(payload, ensure_ascii=False, indent=2))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".wd_out_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _run_worker(req_path: Path, out_path: Path, paths: AppPaths, cfg: AppConfig) -> int:
    try:
        raw = json.loads(req_path.read_text(encoding="utf-8-sig"))
        # Preserve the invoking user's data_dir even when UAC elevates as a separate Admin account
        req_data_dir = raw.get("data_dir")
        if isinstance(req_data_dir, str) and req_data_dir.strip():
            paths = resolve_app_paths_from_data_dir(Path(req_data_dir), exe_dir=paths.exe_dir)
            cfg = load_config(paths)
        action = str(raw.get("action") or "")
        force = bool(raw.get("force", False))
        ext_id = raw.get("ext_id")
        res = execute_action(
            action,
            paths,
            cfg,
            force=force,
            ext_id=str(ext_id) if ext_id else None,
            runner=run_powershell,
            snapshot_fn=collect_system_snapshot,
        )
    except Exception as exc:
        res = ActionResult(
            ok=False,
            code="WORKER_EXCEPTION",
            message=f"提权执行异常: {exc}",
            exit_code=1,
        )

    _atomic_write_json(out_path, res.to_dict())
    return res.exit_code


def _dispatch_privileged_action(
    *,
    action: str,
    paths: AppPaths,
    cfg: AppConfig,
    force: bool = False,
    ext_id: str | None = None,
    elevate: bool = False,
) -> ActionResult:
    if is_admin():
        return execute_action(
            action,
            paths,
            cfg,
            force=force,
            ext_id=ext_id,
            runner=run_powershell,
            snapshot_fn=collect_system_snapshot,
        )

    if not elevate:
        return ActionResult(
            ok=False,
            code="ADMIN_REQUIRED",
            message="此操作需要管理员权限；请在管理员终端运行或附加 --elevate",
            exit_code=3,
        )

    job_token = uuid.uuid4().hex[:10]
    req_path = paths.runtime_dir / f"req_{job_token}.json"
    out_path = paths.runtime_dir / f"res_{job_token}.json"
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    req_path.write_text(
        json.dumps(
            {
                "action": action,
                "force": force,
                "ext_id": ext_id,
                "data_dir": str(paths.data_dir),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        return run_elevated_worker(get_cli_executable(paths.exe_dir), req_path, out_path)
    finally:
        for f in (req_path, out_path):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wiredeck-cli",
        description=f"{APP_NAME} v{APP_VERSION} headless diagnostic & control CLI",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument("--worker", metavar="REQ_JSON", help=argparse.SUPPRESS)
    parser.add_argument("--out", metavar="RES_JSON", help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command")

    p_diag = sub.add_parser("diagnose", help="Run 6-layer wired or wireless diagnosis")
    p_diag.add_argument("--json", action="store_true", dest="as_json", help="Output strict JSON")
    p_diag.add_argument("--transport", choices=["wired", "wireless"], default="wired")
    p_diag.add_argument("--console", choices=sorted(set(ALL_PRESET_IDS) | set(PRESET_ALIASES.keys())), default=None)
    p_diag.add_argument("--no-extensions", action="store_true")
    p_diag.add_argument("--no-network", action="store_true")

    p_status = sub.add_parser("status", help="Show compact mode and WireDeckNat state")
    p_status.add_argument("--json", action="store_true", dest="as_json")
    p_status.add_argument("--check-nat-active", action="store_true", help="Exit 0 if WireDeckNat active, 1 otherwise")
    p_status.add_argument("--check-wifi-tuned", action="store_true", help="Exit 0 if wireless anti-stutter backup is active, 1 otherwise")

    p_apply = sub.add_parser("apply", help="Execute a system action")
    p_apply.add_argument("action", choices=list(SUPPORTED_ACTIONS))
    p_apply.add_argument("--id", dest="ext_id", default=None, help="Extension ID for run-ext")
    p_apply.add_argument("--force", action="store_true")
    p_apply.add_argument("--elevate", action="store_true")
    p_apply.add_argument("--json", action="store_true", dest="as_json")

    p_exp = sub.add_parser("export-bundle", help="Export sanitized diagnostic bundle for DSH/AI")
    p_exp.add_argument("--out", dest="export_out", default=None)
    p_exp.add_argument("--transport", choices=["wired", "wireless"], default=None)
    p_exp.add_argument("--json", action="store_true", dest="as_json")

    p_ext = sub.add_parser("ext", help="Manage DSH/AI hot-reload extensions")
    ext_sub = p_ext.add_subparsers(dest="ext_cmd")

    p_ext_list = ext_sub.add_parser("list", help="List extensions under <data_dir>/extensions")
    p_ext_list.add_argument("--json", action="store_true", dest="as_json")

    p_ext_val = ext_sub.add_parser("validate", help="Validate an extension directory or ID")
    p_ext_val.add_argument("target", help="Extension directory path or extension ID")
    p_ext_val.add_argument("--json", action="store_true", dest="as_json")

    p_ext_run = ext_sub.add_parser("run", help="Run an extension check or fix script")
    p_ext_run.add_argument("ext_id")
    p_ext_run.add_argument("--mode", choices=["check", "fix"], default="check")
    p_ext_run.add_argument("--elevate", action="store_true")
    p_ext_run.add_argument("--json", action="store_true", dest="as_json")

    p_tk = sub.add_parser("toolkit", help="List or run bundled read-only AI/DSH diagnostic toolkits")
    tk_sub = p_tk.add_subparsers(dest="tk_cmd")

    p_tk_list = tk_sub.add_parser("list", help="List bundled common and preset toolkits")
    p_tk_list.add_argument("--preset", default=None)
    p_tk_list.add_argument("--all", action="store_true", dest="show_all")
    p_tk_list.add_argument("--json", action="store_true", dest="as_json")

    p_tk_run = tk_sub.add_parser("run", help="Run a bundled read-only toolkit script")
    p_tk_run.add_argument("tool_id")
    p_tk_run.add_argument("--json", action="store_true", dest="as_json")

    p_plg = sub.add_parser("plugin", help="Manage GitHub patches and plugins")
    plg_sub = p_plg.add_subparsers(dest="plugin_cmd")

    p_plg_list = plg_sub.add_parser("list", help="List installed plugins")
    p_plg_list.add_argument("--json", action="store_true", dest="as_json")

    p_plg_cat = plg_sub.add_parser("catalog", help="Fetch available patches from GitHub registry")
    p_plg_cat.add_argument("--url", default="")
    p_plg_cat.add_argument("--json", action="store_true", dest="as_json")

    p_plg_inst = plg_sub.add_parser("install", help="Install a plugin from GitHub catalog or URL")
    p_plg_inst.add_argument("target", help="Catalog plugin ID, owner/repo, or plugin-package.json URL")
    p_plg_inst.add_argument("--json", action="store_true", dest="as_json")

    p_plg_en = plg_sub.add_parser("enable", help="Enable an installed plugin")
    p_plg_en.add_argument("plugin_id")
    p_plg_en.add_argument("--json", action="store_true", dest="as_json")

    p_plg_dis = plg_sub.add_parser("disable", help="Disable an installed plugin")
    p_plg_dis.add_argument("plugin_id")
    p_plg_dis.add_argument("--json", action="store_true", dest="as_json")

    p_plg_rm = plg_sub.add_parser("remove", help="Uninstall a plugin")
    p_plg_rm.add_argument("plugin_id")
    p_plg_rm.add_argument("--purge", action="store_true")
    p_plg_rm.add_argument("--json", action="store_true", dest="as_json")

    p_rec = sub.add_parser("recorder", help="Inspect external drives, video clips, and recorders")
    rec_sub = p_rec.add_subparsers(dest="rec_cmd")

    for cmd_name, help_txt in (
        ("status", "Show recorder backends and target storage status"),
        ("drives", "List mounted USB/local drives with filesystem and estimated recording hours"),
        ("clips", "List recorded video clips in target output directory"),
        ("offload", "Offload local captures to target output directory with SHA-256 verification"),
        ("prune", "Prune oldest clips in target directory according to storage quota"),
    ):
        sub_p = rec_sub.add_parser(cmd_name, help=help_txt)
        sub_p.add_argument("--dir", dest="rec_dir", default="", help="Optional target output folder override")
        sub_p.add_argument("--json", action="store_true", dest="as_json")

    p_doc = sub.add_parser("doctor", help="Run whole-system readiness & health check across power, GPU, storage, and plugins")
    p_doc.add_argument("--json", action="store_true", dest="as_json")

    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)

    paths = resolve_app_paths()
    cfg = load_config(paths)

    if args.worker:
        if not args.out:
            print("Missing --out for --worker", file=sys.stderr)
            return 1
        return _run_worker(Path(args.worker), Path(args.out), paths, cfg)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "diagnose":
        if args.console:
            cfg.console_profile = args.console
        if getattr(args, "transport", "wired") == "wireless":
            from wiredeck.wireless_diagnostics import run_wireless_diagnosis
            from wiredeck.wireless_probes import collect_wireless_snapshot

            report = run_wireless_diagnosis(
                paths,
                cfg,
                include_extensions=not args.no_extensions,
                probe_network=not args.no_network,
                snapshot_fn=collect_wireless_snapshot,
            )
        else:
            report = run_diagnosis(
                paths,
                cfg,
                include_extensions=not args.no_extensions,
                probe_network=not args.no_network,
                snapshot_fn=collect_system_snapshot,
            )
        lines = [f"{APP_NAME} v{APP_VERSION} | transport={report.transport} | mode={report.mode} | {report.headline}"]
        for layer in report.layers:
            lines.append(f"  [{layer.layer} {layer.name}] {layer.status.value.upper():7s} {layer.summary}")
        _emit(report.to_dict(), args.as_json, "\n".join(lines))
        return 0

    if args.command == "status":
        wifi_tuned = (paths.runtime_dir / "wifi_backup.json").is_file()
        if args.check_wifi_tuned and not args.as_json:
            return 0 if wifi_tuned else 1
        snap = collect_system_snapshot(cfg, probe_network=False)
        report = classify_snapshot(snap, cfg)
        nat_active = any(str(n.get("name", "")) == NAT_NAME for n in (snap.get("net_nats") or []))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "mode": report.mode,
            "can_enable_direct": report.can_enable_direct,
            "nat_active": nat_active,
            "wifi_tuned": wifi_tuned,
            "headline": report.headline,
            "primary_fault": report.primary_fault.to_dict() if report.primary_fault else None,
        }
        if args.check_wifi_tuned:
            if args.as_json:
                _emit(payload, True)
            return 0 if wifi_tuned else 1
        if args.check_nat_active:
            if args.as_json:
                _emit(payload, True)
            return 0 if nat_active else 1
        _emit(payload, args.as_json, f"{report.mode} | nat_active={nat_active} | wifi_tuned={wifi_tuned} | {report.headline}")
        return 0

    if args.command == "apply":
        res = _dispatch_privileged_action(
            action=args.action,
            paths=paths,
            cfg=cfg,
            force=args.force,
            ext_id=args.ext_id,
            elevate=args.elevate,
        )
        _emit(res.to_dict(), args.as_json, f"[{res.code}] {res.message}")
        return res.exit_code

    if args.command == "export-bundle":
        transport = args.transport or cfg.active_transport
        if transport == "wireless":
            from wiredeck.wireless_diagnostics import run_wireless_diagnosis
            from wiredeck.wireless_probes import collect_wireless_snapshot

            report = run_wireless_diagnosis(
                paths,
                cfg,
                include_extensions=True,
                probe_network=True,
                snapshot_fn=collect_wireless_snapshot,
            )
        else:
            report = run_diagnosis(
                paths,
                cfg,
                include_extensions=True,
                probe_network=True,
                snapshot_fn=collect_system_snapshot,
            )
        out_p = Path(args.export_out) if args.export_out else None
        bundle_path, prompt = export_diagnostic_bundle(paths, report, cfg, out_path=out_p)
        payload = {
            "ok": True,
            "bundle_path": str(bundle_path),
            "prompt": prompt,
        }
        _emit(payload, args.as_json, f"Exported: {bundle_path}\n\n{prompt}")
        return 0

    if args.command == "ext":
        if args.ext_cmd == "list":
            entries = inspect_extensions(paths)
            payload = {"extensions_dir": str(paths.extensions_dir), "extensions": entries}
            lines = [f"Extensions ({len(entries)}) in {paths.extensions_dir}:"]
            for e in entries:
                m = e["manifest"]
                lines.append(f"  - {m.get('id')} | valid={e['valid']} | layer={m.get('layer', '-')}")
            _emit(payload, args.as_json, "\n".join(lines))
            return 0

        if args.ext_cmd == "validate":
            target = Path(args.target)
            if not target.exists():
                candidate = paths.extensions_dir / args.target
                if candidate.exists():
                    target = candidate
            manifest, errors = validate_extension_dir(target)
            payload = {
                "valid": manifest is not None and not errors,
                "errors": errors,
                "manifest": manifest.to_dict() if manifest else None,
            }
            _emit(payload, args.as_json, "VALID" if payload["valid"] else "INVALID: " + "; ".join(errors))
            return 0 if payload["valid"] else 1

        if args.ext_cmd == "run":
            manifests = {m.id: m for m in discover_extensions(paths)}
            manifest = manifests.get(args.ext_id)
            if manifest is None:
                res = ActionResult(
                    ok=False, code="EXT_NOT_FOUND", message=f"未找到有效扩展: {args.ext_id}", exit_code=1
                )
                _emit(res.to_dict(), args.as_json, res.message)
                return 1
            if args.mode == "fix" and manifest.requires_admin:
                res = _dispatch_privileged_action(
                    action="run-ext",
                    paths=paths,
                    cfg=cfg,
                    ext_id=manifest.id,
                    elevate=args.elevate,
                )
                _emit(res.to_dict(), args.as_json, f"[{res.code}] {res.message}")
                return res.exit_code
            er = run_extension(manifest, mode=args.mode)
            _emit(er.to_dict(), args.as_json, f"[{er.status.value}] {er.code}: {er.message}")
            return 0 if er.status in (Severity.OK, Severity.IDLE) else 1

        parser.parse_args(["ext", "--help"])
        return 0

    if args.command == "toolkit":
        if args.tk_cmd == "list":
            if args.show_all:
                items = discover_all_toolkits()
                active_p = "all"
            else:
                active_p = normalize_preset_id(args.preset) if args.preset else cfg.active_preset
                items = discover_toolkits(preset_id=active_p, include_common=True)
            payload = {
                "active_preset": active_p,
                "toolkits": [t.to_dict() for t in items],
            }
            lines = [f"Bundled Toolkits ({len(items)}) for preset={active_p}:"]
            for t in items:
                lines.append(f"  - {t.id:34s} [{t.scope}] {t.name_zh}")
            _emit(payload, args.as_json, "\n".join(lines))
            return 0

        if args.tk_cmd == "run":
            res = run_toolkit(args.tool_id, cfg=cfg)
            _emit(res.to_dict(), args.as_json, json.dumps(res.data or {"error": res.error}, ensure_ascii=False, indent=2))
            return 0 if res.ok else 1

        parser.parse_args(["toolkit", "--help"])
        return 0

    if args.command == "plugin":
        if args.plugin_cmd == "list":
            entries = list_installed_plugins(paths)
            payload = {"plugins_dir": str(paths.plugins_dir), "plugins": entries}
            lines = [f"Plugins ({len(entries)}) in {paths.plugins_dir}:"]
            for e in entries:
                m = e.get("manifest") or {}
                lines.append(f"  - {m.get('id')} v{m.get('version', '?')} | valid={e['valid']} | enabled={e['enabled']}")
            _emit(payload, args.as_json, "\n".join(lines))
            return 0

        if args.plugin_cmd == "catalog":
            cat = fetch_plugin_catalog(args.url or cfg.plugin_registry_url)
            lines = [f"Catalog ok={cat['ok']} ({len(cat['plugins'])} entries):"]
            for p in cat["plugins"]:
                lines.append(f"  - {p.get('id')} v{p.get('version')} [{p.get('category', '')}] {p.get('name', '')}")
            _emit(cat, args.as_json, "\n".join(lines))
            return 0 if cat["ok"] else 1

        if args.plugin_cmd == "install":
            t = str(args.target or "").strip()
            is_url = t.startswith(("https://", "http://")) or ("/" in t)
            res = install_plugin_from_github(
                paths,
                plugin_id="" if is_url else t,
                registry_url=cfg.plugin_registry_url,
                direct_url=t if is_url else "",
            )
            _emit(res.to_dict(), args.as_json, f"[{res.code}] {res.message}")
            return res.exit_code

        if args.plugin_cmd in ("enable", "disable"):
            res = set_plugin_enabled(paths, args.plugin_id, args.plugin_cmd == "enable")
            _emit(res.to_dict(), args.as_json, f"[{res.code}] {res.message}")
            return res.exit_code

        if args.plugin_cmd == "remove":
            res = uninstall_plugin(paths, args.plugin_id, remove_config=bool(args.purge))
            _emit(res.to_dict(), args.as_json, f"[{res.code}] {res.message}")
            return res.exit_code

        parser.parse_args(["plugin", "--help"])
        return 0

    if args.command == "recorder":
        target_dir = (getattr(args, "rec_dir", "") or cfg.recorder.output_dir or "").strip()
        if args.rec_cmd == "status":
            drives = list_storage_drives(bitrate_mbps=cfg.recorder.ffmpeg_bitrate_mbps)
            det = detect_recorders(cfg)
            tgt = inspect_target_path(target_dir, bitrate_mbps=cfg.recorder.ffmpeg_bitrate_mbps, min_free_gb=cfg.recorder.min_free_gb, drives=drives)
            payload = {
                "recorder": cfg.recorder.to_dict(),
                "detected": det,
                "target": tgt,
                "effective_split": resolve_effective_split_policy(
                    cfg.recorder.split_mode,
                    cfg.recorder.split_minutes,
                    cfg.recorder.split_size_mb,
                    filesystem=str(tgt.get("filesystem") or ""),
                ),
                "filename_preview": preview_clip_filenames(
                    cfg.recorder.filename_template,
                    target_dir=target_dir,
                    ext=cfg.recorder.ffmpeg_format,
                    preset=cfg.active_preset,
                    backend=str(det.get("effective_backend") or "obs"),
                    mode=cfg.recorder.mode,
                ),
                "drives": drives,
            }
            _emit(payload, args.as_json, f"backend={payload['detected']['effective_backend']} | target={target_dir or '<unset>'}")
            return 0
        if args.rec_cmd == "drives":
            drives = list_storage_drives(bitrate_mbps=cfg.recorder.ffmpeg_bitrate_mbps)
            payload = {"drives": drives}
            lines = [f"Drives ({len(drives)}):"]
            for d in drives:
                lines.append(f"  - {d['device_id']} ({d['filesystem']}, {d['free_gb']} GB free, ~{d['est_hours']}h)")
            _emit(payload, args.as_json, "\n".join(lines))
            return 0
        if args.rec_cmd == "clips":
            res_clips = list_recorded_clips(target_dir)
            _emit(res_clips, args.as_json, f"Clips: {res_clips['total_count']} files ({res_clips['total_gb']} GB) in {target_dir}")
            return 0
        if args.rec_cmd == "offload":
            if not target_dir:
                out = ActionResult(ok=False, code="NO_OUTPUT_DIR", message="未设置目标录像目录", exit_code=1)
                _emit(out.to_dict(), args.as_json, out.message)
                return 1
            watch = [Path(cfg.recorder.source_watch_dir)] if cfg.recorder.source_watch_dir.strip() else [Path.home() / "Videos" / "Captures", Path.home() / "Videos" / "NVIDIA"]
            det = detect_recorders(cfg)
            out = offload_clips_to_target(
                watch,
                Path(target_dir),
                apply_naming=cfg.recorder.apply_naming_on_offload,
                filename_template=cfg.recorder.filename_template,
                preset=cfg.active_preset,
                backend=str(det.get("effective_backend") or "obs"),
                mode=cfg.recorder.mode,
            )
            _emit(out.to_dict(), args.as_json, f"[{out.code}] {out.message}")
            return out.exit_code
        if args.rec_cmd == "prune":
            if not target_dir:
                out = ActionResult(ok=False, code="NO_OUTPUT_DIR", message="未设置目标录像目录", exit_code=1)
                _emit(out.to_dict(), args.as_json, out.message)
                return 1
            out = prune_clips_by_quota(Path(target_dir), max_storage_gb=cfg.recorder.max_storage_gb, min_free_gb=cfg.recorder.min_free_gb)
            _emit(out.to_dict(), args.as_json, f"[{out.code}] {out.message}")
            return out.exit_code
        parser.parse_args(["recorder", "--help"])
        return 0

    if args.command == "doctor":
        sys_env = collect_system_env()
        drives = list_storage_drives(bitrate_mbps=cfg.recorder.ffmpeg_bitrate_mbps)
        det = detect_recorders(cfg, system_env=sys_env)
        t_dir = cfg.recorder.output_dir.strip()
        tgt = inspect_target_path(
            t_dir,
            bitrate_mbps=cfg.recorder.ffmpeg_bitrate_mbps,
            min_free_gb=cfg.recorder.min_free_gb,
            drives=drives,
        )
        eff = resolve_effective_split_policy(
            cfg.recorder.split_mode,
            cfg.recorder.split_minutes,
            cfg.recorder.split_size_mb,
            filesystem=str(tgt.get("filesystem") or ""),
        )
        fb_dir = cfg.recorder.fallback_dir.strip()
        fb_pending = 0
        if fb_dir and fb_dir != t_dir and Path(fb_dir).is_dir():
            fb_pending = int(list_recorded_clips(fb_dir).get("total_count") or 0)
        health = evaluate_system_health(
            system_env=sys_env,
            cfg_dict=cfg.to_dict(),
            recorder_detected=det,
            target_status=tgt,
            effective_split=eff,
            fallback_pending_count=fb_pending,
            installed_plugins=list_installed_plugins(paths),
        )
        lines = [f"{APP_NAME} v{APP_VERSION} Doctor | overall={health['overall']} | warnings={health['warning_count']}"]
        for it in health.get("items") or []:
            mark = "OK" if it.get("ok") else "WARN"
            lines.append(f"  [{mark}] {it.get('label')}: {it.get('value')}")
        for w in health.get("warnings") or []:
            lines.append(f"  ! [{w.get('code')}] {w.get('title')}: {w.get('detail')}")
        _emit(health, args.as_json, "\n".join(lines))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
