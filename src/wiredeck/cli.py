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
from wiredeck.config import AppConfig, export_config_backup, import_config_backup, load_config
from wiredeck.diagnostics import classify_snapshot, run_diagnosis, run_link_benchmark
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
    capture_screenshot,
    detect_recorders,
    inspect_target_path,
    list_recorded_clips,
    list_storage_drives,
    offload_clips_to_target,
    preview_clip_filenames,
    prune_clips_by_quota,
    resolve_capture_target,
    resolve_effective_split_policy,
)
from wiredeck.presets import ALL_PRESET_IDS, PRESET_ALIASES, normalize_preset_id
from wiredeck.probes import collect_system_snapshot
from wiredeck.ps_runner import is_admin, run_elevated_worker, run_powershell
from wiredeck.system_env import collect_system_env, evaluate_system_health
from wiredeck.toolkits import discover_all_toolkits, discover_toolkits, run_toolkit
from wiredeck.uninstaller import discover_uninstall_items
from wiredeck.updater import (
    apply_staged_app_update,
    build_update_manifest,
    check_all_updates,
    download_and_stage_app_update,
    update_all_plugins,
)
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
        ("screenshot", "Capture clean PNG screenshot honoring configured capture scope"),
    ):
        sub_p = rec_sub.add_parser(cmd_name, help=help_txt)
        sub_p.add_argument("--dir", dest="rec_dir", default="", help="Optional target output folder override")
        sub_p.add_argument("--json", action="store_true", dest="as_json")

    p_bench = sub.add_parser("benchmark", help="Run 12-packet RTT/jitter/loss link benchmark & bitrate grading")
    p_bench.add_argument("--transport", choices=["wired", "wireless"], default=None)
    p_bench.add_argument("--ip", default="")
    p_bench.add_argument("--count", type=int, default=12)
    p_bench.add_argument("--json", action="store_true", dest="as_json")

    p_cfg_bk = sub.add_parser("config", help="Export or import WireDeck config and plugin settings backup")
    cfg_sub = p_cfg_bk.add_subparsers(dest="cfg_cmd")
    p_cfg_exp = cfg_sub.add_parser("export", help="Export portable wiredeck-backup/v1 JSON")
    p_cfg_exp.add_argument("out_file")
    p_cfg_exp.add_argument("--json", action="store_true", dest="as_json")
    p_cfg_imp = cfg_sub.add_parser("import", help="Restore config & plugin_configs from wiredeck-backup/v1 JSON")
    p_cfg_imp.add_argument("in_file")
    p_cfg_imp.add_argument("--json", action="store_true", dest="as_json")

    p_doc = sub.add_parser("doctor", help="Run whole-system readiness & health check across power, GPU, storage, and plugins")
    p_doc.add_argument("--json", action="store_true", dest="as_json")

    p_upd = sub.add_parser("update", help="Check or apply WireDeck core & plugin updates")
    upd_sub = p_upd.add_subparsers(dest="update_cmd")

    p_upd_chk = upd_sub.add_parser("check", help="Check core binary & installed plugin updates")
    p_upd_chk.add_argument("--channel", choices=["stable", "beta"], default="")
    p_upd_chk.add_argument("--url", default="")
    p_upd_chk.add_argument("--json", action="store_true", dest="as_json")

    p_upd_app = upd_sub.add_parser("apply", help="Apply available updates (plugins and/or core)")
    p_upd_app.add_argument("--plugins-only", action="store_true")
    p_upd_app.add_argument("--core-only", action="store_true")
    p_upd_app.add_argument("--channel", choices=["stable", "beta"], default="")
    p_upd_app.add_argument("--url", default="")
    p_upd_app.add_argument("--silent", action="store_true")
    p_upd_app.add_argument("--json", action="store_true", dest="as_json")

    p_upd_man = upd_sub.add_parser("manifest", help="Generate wiredeck-update/v1 JSON manifest from release artifacts")
    p_upd_man.add_argument("release_dir", help="Directory containing built setup/portable artifacts")
    p_upd_man.add_argument("--base-url", required=True, help="Public base download URL for release assets")
    p_upd_man.add_argument("--channel", choices=["stable", "beta"], default="stable")
    p_upd_man.add_argument("--notes", default="")
    p_upd_man.add_argument("--release-url", default="")
    p_upd_man.add_argument("--out", default="")
    p_upd_man.add_argument("--json", action="store_true", dest="as_json")

    p_uninst = sub.add_parser("uninstall-scan", help="Scan WireDeck data & optional associated apps (all default OFF)")
    p_uninst.add_argument("--json", action="store_true", dest="as_json")

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
                "capture_target": resolve_capture_target(cfg),
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
        if args.rec_cmd == "screenshot":
            out = capture_screenshot(cfg, output_dir=target_dir)
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

    if args.command == "update":
        if args.update_cmd == "check":
            res_u = check_all_updates(
                paths,
                cfg,
                override_channel=args.channel,
                override_url=args.url,
            )
            app_u = res_u.get("app") or {}
            plg_u = res_u.get("plugins") or {}
            lines = [
                f"{APP_NAME} v{APP_VERSION} Update Check | channel={app_u.get('channel')} | core_update={app_u.get('has_update')} (latest={app_u.get('latest_version') or '-'}) | plugin_updates={plg_u.get('updates_count', 0)}"
            ]
            for pu in plg_u.get("updates_available") or []:
                lines.append(f"  * Plugin {pu.get('id')}: {pu.get('current_version')} -> {pu.get('latest_version')}")
            _emit(res_u, args.as_json, "\n".join(lines))
            return 0 if res_u.get("ok") else 1

        if args.update_cmd == "apply":
            plugin_res_dict: dict[str, Any] | None = None
            core_res_dict: dict[str, Any] | None = None
            overall_ok = True

            if not args.core_only:
                pr = update_all_plugins(paths, cfg)
                plugin_res_dict = pr.to_dict()
                if not pr.ok:
                    overall_ok = False

            if not args.plugins_only:
                chk = check_all_updates(
                    paths,
                    cfg,
                    override_channel=args.channel,
                    override_url=args.url,
                )
                app_u = chk.get("app") or {}
                if not app_u.get("ok"):
                    overall_ok = False
                    core_res_dict = ActionResult(
                        ok=False,
                        code="CORE_CHECK_FAILED",
                        message=str(app_u.get("error") or "无法获取主程序更新"),
                        exit_code=1,
                    ).to_dict()
                elif not app_u.get("has_update"):
                    core_res_dict = ActionResult(
                        ok=True,
                        code="UP_TO_DATE",
                        message=f"WireDeck 已是最新版本 v{APP_VERSION}",
                    ).to_dict()
                else:
                    stg = download_and_stage_app_update(app_u, paths)
                    if not stg.ok:
                        overall_ok = False
                        core_res_dict = stg.to_dict()
                    else:
                        ap = apply_staged_app_update(
                            Path(str(stg.details.get("staged_path") or "")),
                            paths,
                            silent=bool(args.silent),
                        )
                        if not ap.ok:
                            overall_ok = False
                        core_res_dict = ap.to_dict()

            payload = {"ok": overall_ok, "plugins": plugin_res_dict, "core": core_res_dict}
            _emit(payload, args.as_json, json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if overall_ok else 1

        if args.update_cmd == "manifest":
            manifest = build_update_manifest(
                APP_VERSION,
                Path(args.release_dir),
                base_download_url=args.base_url,
                channel=args.channel,
                release_notes=args.notes,
                release_url=args.release_url,
            )
            if args.out:
                out_p = Path(args.out)
                out_p.parent.mkdir(parents=True, exist_ok=True)
                out_p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            _emit(manifest, True if args.as_json else False)
            return 0

        parser.parse_args(["update", "--help"])
        return 0

    if args.command == "uninstall-scan":
        scan = discover_uninstall_items(paths, cfg)
        lines = [
            f"{APP_NAME} v{APP_VERSION} Uninstall Scan (companion cleanup default OFF):",
            f"  WireDeck items: {len(scan.get('wiredeck_items') or [])}",
            f"  Associated apps: {len(scan.get('associated_apps') or [])}",
        ]
        for app_e in scan.get("associated_apps") or []:
            lines.append(f"  - [{app_e.get('category')}] {app_e.get('name')} ({app_e.get('version') or '-'}) default_selected={app_e.get('default_selected')}")
        _emit(scan, args.as_json, "\n".join(lines))
        return 0

    if args.command == "benchmark":
        t_mode = args.transport or cfg.active_transport
        ip_tgt = (args.ip or "").strip() or (cfg.wireless_peer_ip if t_mode == "wireless" else cfg.peer_ip)
        bm = run_link_benchmark(ip_tgt, count=args.count, transport=t_mode)
        txt = f"[{bm.get('grade', '-')}] {bm.get('target_ip')} avg={bm.get('avg_ms')}ms jitter={bm.get('jitter_ms')}ms loss={bm.get('loss_pct')}% | {bm.get('recommended_bitrate')}"
        _emit(bm, args.as_json, txt)
        return 0 if bm.get("ok") else 1

    if args.command == "config":
        if args.cfg_cmd == "export":
            res_ex = export_config_backup(paths, Path(args.out_file), cfg=cfg)
            _emit(res_ex, args.as_json, f"Exported backup to {res_ex.get('path')} ({res_ex.get('plugin_config_count')} plugin configs)")
            return 0 if res_ex.get("ok") else 1
        if args.cfg_cmd == "import":
            res_im = import_config_backup(paths, Path(args.in_file))
            _emit(res_im, args.as_json, json.dumps(res_im, ensure_ascii=False, indent=2))
            return 0 if res_im.get("ok") else 1
        parser.parse_args(["config", "--help"])
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
