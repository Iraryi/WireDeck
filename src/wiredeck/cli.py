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
from wiredeck.probes import collect_system_snapshot
from wiredeck.ps_runner import is_admin, run_elevated_worker, run_powershell
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

    p_diag = sub.add_parser("diagnose", help="Run 6-layer wired direct diagnosis")
    p_diag.add_argument("--json", action="store_true", dest="as_json", help="Output strict JSON")
    p_diag.add_argument("--console", choices=["xbox", "ps5", "generic"], default=None)
    p_diag.add_argument("--no-extensions", action="store_true")
    p_diag.add_argument("--no-network", action="store_true")

    p_status = sub.add_parser("status", help="Show compact mode and WireDeckNat state")
    p_status.add_argument("--json", action="store_true", dest="as_json")
    p_status.add_argument("--check-nat-active", action="store_true", help="Exit 0 if WireDeckNat active, 1 otherwise")

    p_apply = sub.add_parser("apply", help="Execute a system action")
    p_apply.add_argument("action", choices=list(SUPPORTED_ACTIONS))
    p_apply.add_argument("--id", dest="ext_id", default=None, help="Extension ID for run-ext")
    p_apply.add_argument("--force", action="store_true")
    p_apply.add_argument("--elevate", action="store_true")
    p_apply.add_argument("--json", action="store_true", dest="as_json")

    p_exp = sub.add_parser("export-bundle", help="Export sanitized diagnostic bundle for DSH/AI")
    p_exp.add_argument("--out", dest="export_out", default=None)
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
        report = run_diagnosis(
            paths,
            cfg,
            include_extensions=not args.no_extensions,
            probe_network=not args.no_network,
            snapshot_fn=collect_system_snapshot,
        )
        lines = [f"{APP_NAME} v{APP_VERSION} | mode={report.mode} | {report.headline}"]
        for layer in report.layers:
            lines.append(f"  [{layer.layer} {layer.name}] {layer.status.value.upper():7s} {layer.summary}")
        _emit(report.to_dict(), args.as_json, "\n".join(lines))
        return 0

    if args.command == "status":
        snap = collect_system_snapshot(cfg, probe_network=False)
        report = classify_snapshot(snap, cfg)
        nat_active = any(str(n.get("name", "")) == NAT_NAME for n in (snap.get("net_nats") or []))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "mode": report.mode,
            "can_enable_direct": report.can_enable_direct,
            "nat_active": nat_active,
            "headline": report.headline,
            "primary_fault": report.primary_fault.to_dict() if report.primary_fault else None,
        }
        if args.check_nat_active:
            if args.as_json:
                _emit(payload, True)
            return 0 if nat_active else 1
        _emit(payload, args.as_json, f"{report.mode} | nat_active={nat_active} | {report.headline}")
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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
