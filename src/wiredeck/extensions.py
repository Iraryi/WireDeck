"""Hot-reload DSH/AI extension engine with schema validation, traversal guard, and timeout isolation."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Literal

from wiredeck.models import ExtensionManifest, ExtensionRunResult, Severity
from wiredeck.paths import AppPaths, ensure_within
from wiredeck.ps_runner import run_ps_file
from wiredeck.version import EXT_SCHEMA_VERSION

VALID_LAYERS = ("L1", "L2", "L3", "L4", "L5", "L6")
VALID_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MIN_TIMEOUT_SEC = 1
MAX_TIMEOUT_SEC = 15


def _clamp_timeout(raw: Any, default: int = 5) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = default
    return max(MIN_TIMEOUT_SEC, min(MAX_TIMEOUT_SEC, val))


def _validate_script_ref(ext_dir: Path, field_name: str, rel_value: Any) -> tuple[str | None, str | None]:
    if rel_value is None or rel_value == "":
        return None, None
    if not isinstance(rel_value, str):
        return None, f"{field_name} must be a relative script filename"

    raw_path = Path(rel_value)
    if raw_path.is_absolute():
        return None, f"{field_name} rejects absolute path (path traversal not allowed): {rel_value}"

    try:
        resolved = ensure_within(ext_dir, ext_dir / raw_path)
    except ValueError as exc:
        return None, f"{field_name} path traversal error: {exc}"

    if resolved.suffix.lower() != ".ps1":
        return None, f"{field_name} must point to a .ps1 file: {rel_value}"

    if not resolved.is_file():
        return None, f"{field_name} file not found inside extension directory: {rel_value}"

    return rel_value, None


def validate_extension_dir(ext_dir: Path) -> tuple[ExtensionManifest | None, list[str]]:
    """Validate an extension directory and its ext.json manifest."""
    ext_dir = Path(ext_dir).resolve()
    errors: list[str] = []
    manifest_file = ext_dir / "ext.json"

    if not ext_dir.is_dir():
        return None, [f"Extension directory does not exist: {ext_dir}"]
    if not manifest_file.is_file():
        return None, [f"Missing ext.json in {ext_dir}"]

    try:
        raw = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return None, [f"Invalid JSON in ext.json: {exc}"]

    if not isinstance(raw, dict):
        return None, ["ext.json root must be a JSON object"]

    schema = str(raw.get("schema", ""))
    if schema != EXT_SCHEMA_VERSION:
        errors.append(f"Unsupported schema '{schema}' (expected '{EXT_SCHEMA_VERSION}')")

    ext_id = str(raw.get("id", "")).strip()
    if not VALID_ID_RE.match(ext_id):
        errors.append(f"Invalid extension id '{ext_id}'")

    name = str(raw.get("name", "")).strip()
    if not name:
        errors.append("Missing required field 'name'")

    layer = str(raw.get("layer", "L1")).strip().upper()
    if layer not in VALID_LAYERS:
        errors.append(f"Invalid layer '{layer}' (must be one of {', '.join(VALID_LAYERS)})")

    check_script, check_err = _validate_script_ref(ext_dir, "check_script", raw.get("check_script"))
    if check_err:
        errors.append(check_err)
    fix_script, fix_err = _validate_script_ref(ext_dir, "fix_script", raw.get("fix_script"))
    if fix_err:
        errors.append(fix_err)

    if not check_script and not fix_script:
        errors.append("Extension must define at least one of 'check_script' or 'fix_script'")

    overrides_raw = raw.get("overrides_faults") or []
    if not isinstance(overrides_raw, list) or not all(isinstance(x, str) for x in overrides_raw):
        errors.append("overrides_faults must be a list of fault code strings")
        overrides: list[str] = []
    else:
        overrides = [x.strip() for x in overrides_raw if x.strip()]

    if errors:
        return None, errors

    manifest = ExtensionManifest(
        schema=schema,
        id=ext_id,
        name=name,
        author=str(raw.get("author", "User")).strip() or "User",
        enabled=bool(raw.get("enabled", True)),
        auto_check=bool(raw.get("auto_check", False)),
        layer=layer,
        overrides_faults=overrides,
        requires_admin=bool(raw.get("requires_admin", False)),
        timeout_sec=_clamp_timeout(raw.get("timeout_sec"), default=5),
        check_script=check_script,
        fix_script=fix_script,
        description=str(raw.get("description", "")).strip(),
        ext_dir=ext_dir,
    )
    return manifest, []


def discover_extensions(paths: AppPaths) -> list[ExtensionManifest]:
    """Return all valid extension manifests under <data_dir>/extensions/."""
    manifests: list[ExtensionManifest] = []
    if not paths.extensions_dir.is_dir():
        return manifests

    for child in sorted(paths.extensions_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        manifest, errors = validate_extension_dir(child)
        if manifest is not None and not errors:
            manifests.append(manifest)
    return manifests


def inspect_extensions(paths: AppPaths) -> list[dict[str, Any]]:
    """Return structured status for every extension folder (including invalid ones)."""
    entries: list[dict[str, Any]] = []
    if not paths.extensions_dir.is_dir():
        return entries

    for child in sorted(paths.extensions_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        manifest, errors = validate_extension_dir(child)
        if manifest is not None:
            entries.append({"valid": True, "errors": [], "manifest": manifest.to_dict()})
        else:
            entries.append(
                {
                    "valid": False,
                    "errors": errors,
                    "manifest": {"id": child.name, "ext_dir": str(child)},
                }
            )
    return entries


def _parse_ext_json(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    # Try whole stdout first, then from first '{', then last non-empty JSON line
    for candidate in (text, text[text.find("{") :] if "{" in text else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
    return None


def _coerce_severity(raw: Any) -> Severity:
    val = str(raw or "ok").strip().lower()
    mapping = {
        "ok": Severity.OK,
        "warn": Severity.WARN,
        "warning": Severity.WARN,
        "fail": Severity.FAIL,
        "error": Severity.FAIL,
        "idle": Severity.IDLE,
        "skipped": Severity.SKIPPED,
    }
    return mapping.get(val, Severity.WARN)


def run_extension(
    manifest: ExtensionManifest,
    mode: Literal["check", "fix"] = "check",
    timeout_override: int | None = None,
) -> ExtensionRunResult:
    """Run a validated extension script in an isolated subprocess with hard timeout."""
    script_rel = manifest.check_script if mode == "check" else manifest.fix_script
    if not script_rel:
        return ExtensionRunResult(
            ext_id=manifest.id,
            name=manifest.name,
            author=manifest.author,
            layer=manifest.layer,
            status=Severity.WARN,
            code="EXT_ERROR",
            title=f"{manifest.name[:10]} 无脚本",
            message=f"扩展未提供 {mode}_script",
            fix_available=bool(manifest.fix_script),
            requires_admin=manifest.requires_admin,
        )

    try:
        script_path = ensure_within(manifest.ext_dir, manifest.ext_dir / script_rel)
    except ValueError as exc:
        return ExtensionRunResult(
            ext_id=manifest.id,
            name=manifest.name,
            author=manifest.author,
            layer=manifest.layer,
            status=Severity.WARN,
            code="EXT_ERROR",
            title="扩展路径非法",
            message=str(exc),
        )

    timeout = _clamp_timeout(
        timeout_override if timeout_override is not None else manifest.timeout_sec,
        default=manifest.timeout_sec,
    )
    t0 = time.perf_counter()
    ps_res = run_ps_file(
        script_path,
        timeout=timeout,
        extra_env={
            "WIREDECK_EXT_ID": manifest.id,
            "WIREDECK_EXT_MODE": mode,
            "WIREDECK_EXT_DIR": str(manifest.ext_dir),
        },
        cwd=manifest.ext_dir,
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if ps_res.timed_out:
        return ExtensionRunResult(
            ext_id=manifest.id,
            name=manifest.name,
            author=manifest.author,
            layer=manifest.layer,
            status=Severity.WARN,
            code="EXT_ERROR",
            title="扩展执行超时",
            message=f"扩展 {manifest.id} 超过 {timeout}s 未返回",
            fix_available=bool(manifest.fix_script),
            requires_admin=manifest.requires_admin,
            duration_ms=elapsed_ms,
        )

    parsed = _parse_ext_json(ps_res.stdout)
    if parsed is None:
        return ExtensionRunResult(
            ext_id=manifest.id,
            name=manifest.name,
            author=manifest.author,
            layer=manifest.layer,
            status=Severity.WARN,
            code="EXT_ERROR",
            title="扩展输出异常",
            message=f"扩展 {manifest.id} 未输出合法 JSON",
            fix_available=bool(manifest.fix_script),
            requires_admin=manifest.requires_admin,
            details={
                "returncode": ps_res.returncode,
                "stdout_preview": ps_res.stdout[:200],
                "stderr_preview": ps_res.stderr[:200],
            },
            duration_ms=elapsed_ms,
        )

    return ExtensionRunResult(
        ext_id=manifest.id,
        name=manifest.name,
        author=manifest.author,
        layer=manifest.layer,
        status=_coerce_severity(parsed.get("status")),
        code=str(parsed.get("code") or f"EXT_{manifest.id.upper()}"),
        title=str(parsed.get("title") or manifest.name),
        message=str(parsed.get("message") or ""),
        fix_available=bool(manifest.fix_script),
        requires_admin=manifest.requires_admin,
        overrides_faults=list(manifest.overrides_faults),
        details=dict(parsed.get("details") or {}) if isinstance(parsed.get("details"), dict) else {},
        duration_ms=elapsed_ms,
    )


def run_auto_check_extensions(paths: AppPaths) -> list[ExtensionRunResult]:
    """Run all enabled extensions with auto_check=True safely."""
    results: list[ExtensionRunResult] = []
    for manifest in discover_extensions(paths):
        if manifest.enabled and manifest.auto_check and manifest.check_script:
            results.append(run_extension(manifest, mode="check"))
    return results


def run_extension_by_id(
    paths: AppPaths,
    ext_id: str,
    mode: Literal["check", "fix"] = "check",
) -> ExtensionRunResult:
    """Find an extension by id and execute it."""
    for manifest in discover_extensions(paths):
        if manifest.id == ext_id:
            return run_extension(manifest, mode=mode)
    return ExtensionRunResult(
        ext_id=ext_id,
        name=ext_id,
        author="",
        layer="L1",
        status=Severity.FAIL,
        code="EXT_NOT_FOUND",
        title="未找到扩展",
        message=f"扩展目录中未找到有效扩展: {ext_id}",
    )
