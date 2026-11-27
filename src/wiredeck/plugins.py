"""WireDeck v2 Plugin & GitHub Patch Engine.

Features:
- Schema validation (`wiredeck-plugin/v2`) with strict path-traversal guards
- 13 lifecycle hooks + declarative `host_commands` (process, window, hotkey, toast)
- Typed `settings_schema` persisted independently in `plugin_configs/<id>.json`
- SHA-256 verified GitHub registry catalog & direct repo/URL patch installer
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from wiredeck.models import ActionResult, Severity
from wiredeck.paths import AppPaths, ensure_within
from wiredeck.ps_runner import run_ps_file
from wiredeck.stream_client import download_and_verify_asset
from wiredeck.version import (
    APP_VERSION,
    DEFAULT_PLUGIN_REGISTRY_URL,
    PLUGIN_REGISTRY_SCHEMA,
    PLUGIN_SCHEMA_VERSION,
)
from wiredeck.win32_helpers import Win32Ops, default_win32_ops

SUPPORTED_HOOK_EVENTS: tuple[str, ...] = (
    "on_app_start",
    "on_app_exit",
    "before_quick_play",
    "after_quick_play",
    "before_transport_enable",
    "after_transport_enable",
    "before_transport_disable",
    "after_transport_disable",
    "before_client_launch",
    "after_client_launch",
    "on_client_exit",
    "before_quick_restore",
    "after_quick_restore",
)

SUPPORTED_CAPABILITIES: tuple[str, ...] = (
    "video-enhance",
    "translation",
    "recording",
    "input-remap",
    "audio",
    "overlay",
    "network",
    "diagnostics",
    "automation",
)

SUPPORTED_HOST_COMMANDS: tuple[str, ...] = (
    "launch_process",
    "restart_process",
    "wait_window",
    "sleep",
    "focus_window",
    "send_hotkey",
    "notify",
    "open_url",
    "stop_process",
    "keep_awake",
    "set_gpu_preference",
    "download_file",
    "recorder_action",
)

VALID_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MIN_PLUGIN_TIMEOUT = 1
MAX_PLUGIN_TIMEOUT = 30
VALID_FIELD_TYPES = ("text", "select", "dynamic_select", "bool", "hotkey", "file_path", "number", "multi_preset")


@dataclass
class PluginSettingField:
    key: str
    type: str
    label: str
    default: Any = ""
    description: str = ""
    options: list[dict[str, str]] = field(default_factory=list)
    options_script: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    file_filter: str = ""
    placeholder: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PluginActionDef:
    id: str
    label: str
    script: str
    description: str = ""
    style: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PluginManifest:
    schema: str
    id: str
    name: str
    version: str
    author: str
    category: str
    description: str
    homepage: str = ""
    capabilities: list[str] = field(default_factory=list)
    preset_scope: list[str] = field(default_factory=lambda: ["all"])
    enabled_by_default: bool = True
    requires_admin: bool = False
    timeout_sec: int = 10
    hooks: dict[str, str] = field(default_factory=dict)
    actions: list[PluginActionDef] = field(default_factory=list)
    settings_schema: list[PluginSettingField] = field(default_factory=list)
    diagnostic_check: str | None = None
    requirements: dict[str, Any] = field(default_factory=dict)
    plugin_dir: Path = field(default_factory=lambda: Path("."))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "category": self.category,
            "description": self.description,
            "homepage": self.homepage,
            "capabilities": list(self.capabilities),
            "preset_scope": list(self.preset_scope),
            "enabled_by_default": self.enabled_by_default,
            "requires_admin": self.requires_admin,
            "timeout_sec": self.timeout_sec,
            "hooks": dict(self.hooks),
            "actions": [a.to_dict() for a in self.actions],
            "settings_schema": [f.to_dict() for f in self.settings_schema],
            "diagnostic_check": self.diagnostic_check,
            "requirements": dict(self.requirements),
            "plugin_dir": str(self.plugin_dir),
        }


@dataclass
class PluginHookResult:
    plugin_id: str
    plugin_name: str
    event: str
    ok: bool
    status: Severity
    code: str
    message: str
    host_commands_executed: list[dict[str, Any]] = field(default_factory=list)
    saved_settings: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "plugin_name": self.plugin_name,
            "event": self.event,
            "ok": self.ok,
            "status": self.status.value,
            "code": self.code,
            "message": self.message,
            "host_commands_executed": list(self.host_commands_executed),
            "saved_settings": dict(self.saved_settings),
            "details": dict(self.details),
            "duration_ms": self.duration_ms,
        }


def compare_versions(v1: str, v2: str) -> int:
    """Compare semantic version strings; return -1 if v1 < v2, 0 if equal, 1 if v1 > v2."""

    def _parts(raw: str) -> tuple[int, ...]:
        cleaned = (raw or "0").strip().lstrip("vV")
        nums: list[int] = []
        for seg in re.split(r"[.\-+]", cleaned):
            m = re.match(r"^(\d+)", seg)
            if m:
                nums.append(int(m.group(1)))
        return tuple(nums or [0])

    p1, p2 = _parts(v1), _parts(v2)
    max_len = max(len(p1), len(p2))
    p1 += (0,) * (max_len - len(p1))
    p2 += (0,) * (max_len - len(p2))
    if p1 < p2:
        return -1
    if p1 > p2:
        return 1
    return 0


def _clamp_timeout(raw: Any, default: int = 10) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = default
    return max(MIN_PLUGIN_TIMEOUT, min(MAX_PLUGIN_TIMEOUT, val))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".wd_plg_", suffix=".tmp", dir=str(path.parent))
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


def _validate_ps1_ref(plugin_dir: Path, field_name: str, rel_value: Any) -> tuple[str | None, str | None]:
    if rel_value is None or rel_value == "":
        return None, None
    if not isinstance(rel_value, str):
        return None, f"{field_name} must be a relative script path"
    raw_path = Path(rel_value)
    if raw_path.is_absolute():
        return None, f"{field_name} rejects absolute path (traversal not allowed): {rel_value}"
    try:
        resolved = ensure_within(plugin_dir, plugin_dir / raw_path)
    except ValueError as exc:
        return None, f"{field_name} path traversal error: {exc}"
    if resolved.suffix.lower() != ".ps1":
        return None, f"{field_name} must point to a .ps1 file: {rel_value}"
    if not resolved.is_file():
        return None, f"{field_name} script not found inside plugin directory: {rel_value}"
    return rel_value, None


def _parse_settings_schema(raw_schema: Any, errors: list[str], plugin_dir: Path | None = None) -> list[PluginSettingField]:
    if raw_schema is None:
        return []
    if not isinstance(raw_schema, list):
        errors.append("settings_schema must be a list")
        return []
    fields: list[PluginSettingField] = []
    for item in raw_schema:
        if not isinstance(item, dict):
            errors.append("settings_schema entry must be an object")
            continue
        key = str(item.get("key", "")).strip()
        ftype = str(item.get("type", "text")).strip()
        label = str(item.get("label", key)).strip() or key
        if not key or not re.match(r"^[A-Za-z0-9_.-]+$", key):
            errors.append(f"Invalid setting key: {key!r}")
            continue
        if ftype not in VALID_FIELD_TYPES:
            ftype = "text"
        opt_script = ""
        if item.get("options_script") and plugin_dir is not None:
            ref, err = _validate_ps1_ref(plugin_dir, f"settings_schema.{key}.options_script", item.get("options_script"))
            if err:
                errors.append(err)
            elif ref:
                opt_script = ref
        elif isinstance(item.get("options_script"), str):
            opt_script = item.get("options_script", "").strip()
        opts: list[dict[str, str]] = []
        raw_opts = item.get("options") or []
        if isinstance(raw_opts, list):
            for opt in raw_opts:
                if isinstance(opt, dict):
                    opts.append({"value": str(opt.get("value", "")), "label": str(opt.get("label", opt.get("value", "")))})
                elif isinstance(opt, str):
                    opts.append({"value": opt, "label": opt})
        fields.append(
            PluginSettingField(
                key=key,
                type=ftype,
                label=label,
                default=item.get("default", False if ftype == "bool" else ([] if ftype == "multi_preset" else "")),
                description=str(item.get("description", "")).strip(),
                options=opts,
                options_script=opt_script,
                min=float(item["min"]) if isinstance(item.get("min"), (int, float)) else None,
                max=float(item["max"]) if isinstance(item.get("max"), (int, float)) else None,
                step=float(item["step"]) if isinstance(item.get("step"), (int, float)) else None,
                file_filter=str(item.get("file_filter", "")).strip(),
                placeholder=str(item.get("placeholder", "")).strip(),
            )
        )
    return fields


def validate_plugin_dir(plugin_dir: Path) -> tuple[PluginManifest | None, list[str]]:
    """Validate a plugin directory and its `plugin.json` manifest strictly."""
    plugin_dir = Path(plugin_dir).resolve()
    errors: list[str] = []
    manifest_file = plugin_dir / "plugin.json"

    if not plugin_dir.is_dir():
        return None, [f"Plugin directory does not exist: {plugin_dir}"]
    if not manifest_file.is_file():
        return None, [f"Missing plugin.json in {plugin_dir}"]

    try:
        raw = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return None, [f"Invalid JSON in plugin.json: {exc}"]

    if not isinstance(raw, dict):
        return None, ["plugin.json root must be a JSON object"]

    schema = str(raw.get("schema", ""))
    if schema != PLUGIN_SCHEMA_VERSION:
        errors.append(f"Unsupported schema '{schema}' (expected '{PLUGIN_SCHEMA_VERSION}')")

    plugin_id = str(raw.get("id", "")).strip()
    if not VALID_PLUGIN_ID_RE.match(plugin_id):
        errors.append(f"Invalid plugin id '{plugin_id}'")

    name = str(raw.get("name", "")).strip()
    if not name:
        errors.append("Missing required field 'name'")

    version = str(raw.get("version", "")).strip()
    if not version:
        errors.append("Missing required field 'version'")

    raw_hooks = raw.get("hooks") or {}
    hooks: dict[str, str] = {}
    if not isinstance(raw_hooks, dict):
        errors.append("hooks must be an object mapping hook event -> relative .ps1 script")
    else:
        for ev, rel in raw_hooks.items():
            if ev not in SUPPORTED_HOOK_EVENTS:
                errors.append(f"Unsupported hook event '{ev}'")
                continue
            ref, err = _validate_ps1_ref(plugin_dir, f"hooks.{ev}", rel)
            if err:
                errors.append(err)
            elif ref:
                hooks[ev] = ref

    actions: list[PluginActionDef] = []
    raw_actions = raw.get("actions") or []
    if not isinstance(raw_actions, list):
        errors.append("actions must be a list")
    else:
        for idx, item in enumerate(raw_actions):
            if not isinstance(item, dict):
                errors.append(f"actions[{idx}] must be an object")
                continue
            act_id = str(item.get("id", "")).strip()
            act_lbl = str(item.get("label", act_id)).strip()
            if not act_id:
                errors.append(f"actions[{idx}] missing id")
                continue
            ref, err = _validate_ps1_ref(plugin_dir, f"actions.{act_id}", item.get("script"))
            if err:
                errors.append(err)
            elif not ref:
                errors.append(f"actions.{act_id} missing script")
            else:
                actions.append(
                    PluginActionDef(
                        id=act_id,
                        label=act_lbl or act_id,
                        script=ref,
                        description=str(item.get("description", "")).strip(),
                        style=str(item.get("style", "default")).strip() or "default",
                    )
                )

    diag_check, diag_err = _validate_ps1_ref(plugin_dir, "diagnostic_check", raw.get("diagnostic_check"))
    if diag_err:
        errors.append(diag_err)

    schema_fields = _parse_settings_schema(raw.get("settings_schema"), errors, plugin_dir=plugin_dir)

    if not hooks and not actions and not diag_check:
        errors.append("Plugin must define at least one hook, action, or diagnostic_check")

    caps_raw = raw.get("capabilities") or []
    caps = [str(c).strip() for c in caps_raw if isinstance(c, str) and c.strip()] if isinstance(caps_raw, list) else []
    scope_raw = raw.get("preset_scope") or ["all"]
    preset_scope = (
        [str(s).strip() for s in scope_raw if isinstance(s, str) and s.strip()]
        if isinstance(scope_raw, list)
        else ["all"]
    )
    reqs = dict(raw.get("requirements") or {}) if isinstance(raw.get("requirements"), dict) else {}

    if errors:
        return None, errors

    manifest = PluginManifest(
        schema=schema,
        id=plugin_id,
        name=name,
        version=version,
        author=str(raw.get("author", "WireDeck Community")).strip() or "WireDeck Community",
        category=str(raw.get("category", caps[0] if caps else "automation")).strip() or "automation",
        description=str(raw.get("description", "")).strip(),
        homepage=str(raw.get("homepage", "")).strip(),
        capabilities=caps,
        preset_scope=preset_scope or ["all"],
        enabled_by_default=bool(raw.get("enabled_by_default", True)),
        requires_admin=bool(raw.get("requires_admin", False)),
        timeout_sec=_clamp_timeout(raw.get("timeout_sec"), default=10),
        hooks=hooks,
        actions=actions,
        settings_schema=schema_fields,
        diagnostic_check=diag_check,
        requirements=reqs,
        plugin_dir=plugin_dir,
    )
    return manifest, []


def _plugin_config_path(paths: AppPaths, plugin_id: str) -> Path:
    safe_id = Path(plugin_id).name
    return paths.plugin_configs_dir / f"{safe_id}.json"


def _default_settings_from_manifest(manifest: PluginManifest) -> dict[str, Any]:
    return {f.key: (list(f.default) if isinstance(f.default, list) else f.default) for f in manifest.settings_schema}


def load_plugin_config(paths: AppPaths, manifest: PluginManifest) -> dict[str, Any]:
    """Load persisted plugin enabled state & settings merged with schema defaults."""
    defaults = _default_settings_from_manifest(manifest)
    cfg_path = _plugin_config_path(paths, manifest.id)
    if not cfg_path.is_file():
        return {"enabled": manifest.enabled_by_default, "settings": defaults}
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            return {"enabled": manifest.enabled_by_default, "settings": defaults}
        enabled = bool(raw.get("enabled", manifest.enabled_by_default))
        stored = raw.get("settings")
        merged = dict(defaults)
        if isinstance(stored, dict):
            for k, v in stored.items():
                merged[k] = v
        return {"enabled": enabled, "settings": merged}
    except Exception:
        return {"enabled": manifest.enabled_by_default, "settings": defaults}


def save_plugin_config(
    paths: AppPaths,
    plugin_id: str,
    *,
    enabled: bool | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically save plugin enabled state and/or settings without touching plugin files."""
    cfg_path = _plugin_config_path(paths, plugin_id)
    current: dict[str, Any] = {"enabled": True, "settings": {}}
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                current["enabled"] = bool(raw.get("enabled", True))
                if isinstance(raw.get("settings"), dict):
                    current["settings"] = dict(raw["settings"])
        except Exception:
            pass
    if enabled is not None:
        current["enabled"] = bool(enabled)
    if settings is not None and isinstance(settings, dict):
        merged = dict(current["settings"])
        merged.update(settings)
        current["settings"] = merged
    _atomic_write_json(cfg_path, current)
    return current


def set_plugin_enabled(paths: AppPaths, plugin_id: str, enabled: bool) -> ActionResult:
    manifests = {m.id: m for m in discover_plugins(paths)}
    if plugin_id not in manifests:
        return ActionResult(ok=False, code="PLUGIN_NOT_FOUND", message=f"未找到插件：{plugin_id}", exit_code=1)
    saved = save_plugin_config(paths, plugin_id, enabled=bool(enabled))
    return ActionResult(
        ok=True,
        code="PLUGIN_ENABLED" if saved["enabled"] else "PLUGIN_DISABLED",
        message=f"已{'启用' if saved['enabled'] else '停用'}插件：{manifests[plugin_id].name}",
        details={"plugin_id": plugin_id, "enabled": saved["enabled"]},
    )


def discover_plugins(paths: AppPaths) -> list[PluginManifest]:
    """Return all valid v2 plugins under `paths.plugins_dir`."""
    manifests: list[PluginManifest] = []
    if not paths.plugins_dir.is_dir():
        return manifests
    for child in sorted(paths.plugins_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest, errors = validate_plugin_dir(child)
        if manifest is not None and not errors:
            manifests.append(manifest)
    return manifests


def _read_install_meta(plugin_dir: Path) -> dict[str, Any]:
    meta_file = plugin_dir / "_install_meta.json"
    if not meta_file.is_file():
        return {}
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def list_installed_plugins(paths: AppPaths) -> list[dict[str, Any]]:
    """Return detailed status for every directory under `paths.plugins_dir`."""
    entries: list[dict[str, Any]] = []
    if not paths.plugins_dir.is_dir():
        return entries
    for child in sorted(paths.plugins_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest, errors = validate_plugin_dir(child)
        if manifest is not None:
            cfg_state = load_plugin_config(paths, manifest)
            entries.append(
                {
                    "valid": True,
                    "errors": [],
                    "enabled": cfg_state["enabled"],
                    "settings": cfg_state["settings"],
                    "install_meta": _read_install_meta(child),
                    "manifest": manifest.to_dict(),
                }
            )
        else:
            entries.append(
                {
                    "valid": False,
                    "errors": errors,
                    "enabled": False,
                    "settings": {},
                    "install_meta": _read_install_meta(child),
                    "manifest": {"id": child.name, "name": child.name, "plugin_dir": str(child)},
                }
            )
    return entries


def has_enabled_hook(paths: AppPaths, event: str) -> bool:
    """Fast check used by `--silent` launcher to decide if it needs to await client exit."""
    for m in discover_plugins(paths):
        if event not in m.hooks:
            continue
        if load_plugin_config(paths, m)["enabled"]:
            return True
    return False


def get_active_plugin_badges(paths: AppPaths, preset_id: str) -> list[dict[str, Any]]:
    """Return lightweight descriptors of enabled plugins relevant to the given preset."""
    badges: list[dict[str, Any]] = []
    for m in discover_plugins(paths):
        cfg_state = load_plugin_config(paths, m)
        if not cfg_state["enabled"]:
            continue
        scope = m.preset_scope or ["all"]
        enabled_presets = cfg_state["settings"].get("enabled_presets")
        if isinstance(enabled_presets, list) and enabled_presets:
            if "all" not in enabled_presets and preset_id not in enabled_presets:
                continue
        elif "all" not in scope and preset_id not in scope:
            continue
        badges.append(
            {
                "id": m.id,
                "name": m.name,
                "version": m.version,
                "category": m.category,
                "capabilities": list(m.capabilities),
            }
        )
    return badges


def execute_host_commands(
    commands: list[dict[str, Any]],
    *,
    context: dict[str, Any],
    win32_ops: Win32Ops | None = None,
) -> list[dict[str, Any]]:
    """Execute declarative Win32/OS host commands returned by a plugin script safely."""
    ops = win32_ops or default_win32_ops()
    results: list[dict[str, Any]] = []
    last_hwnd = 0

    for cmd in commands:
        if not isinstance(cmd, dict):
            continue
        kind = str(cmd.get("cmd", "")).strip()
        if kind not in SUPPORTED_HOST_COMMANDS:
            results.append({"cmd": kind, "ok": False, "error": "unsupported_command"})
            continue

        try:
            if kind == "launch_process":
                exe = str(cmd.get("exe", "")).strip()
                raw_args = cmd.get("args") or []
                args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
                cwd = str(cmd.get("cwd", "")).strip()
                minimized = bool(cmd.get("minimized", False))
                if_not_running = bool(cmd.get("if_not_running", False))
                exe_name = Path(exe).name if exe else ""
                if not exe:
                    results.append({"cmd": kind, "ok": False, "error": "missing_exe"})
                    continue
                if if_not_running and exe_name and ops.is_process_running(exe_name):
                    results.append({"cmd": kind, "ok": True, "skipped": "already_running", "exe": exe})
                    continue
                pid = ops.launch_process(exe, args=args, cwd=cwd, minimized=minimized)
                results.append({"cmd": kind, "ok": True, "pid": pid, "exe": exe})

            elif kind == "wait_window":
                target_pid = cmd.get("pid")
                if target_pid is None and not cmd.get("process_name") and not cmd.get("title_contains"):
                    target_pid = context.get("client_pid") or None
                proc_name = str(cmd.get("process_name") or (context.get("client_exe") if not target_pid else "") or "")
                title_sub = str(cmd.get("title_contains") or "")
                timeout_sec = max(0.5, min(30.0, float(cmd.get("timeout_sec", 15.0))))
                hwnd = ops.wait_window(
                    pid=int(target_pid) if target_pid else None,
                    process_name=proc_name,
                    title_contains=title_sub,
                    timeout_sec=timeout_sec,
                    poll_interval=0.25,
                )
                if not hwnd and target_pid and (cmd.get("fallback_process_name") or context.get("client_exe")):
                    fb_proc = str(cmd.get("fallback_process_name") or context.get("client_exe") or "")
                    hwnd = ops.wait_window(pid=None, process_name=fb_proc, title_contains=title_sub, timeout_sec=3.0)
                if hwnd:
                    last_hwnd = hwnd
                results.append({"cmd": kind, "ok": bool(hwnd), "hwnd": hwnd})

            elif kind == "sleep":
                sec = max(0.0, min(30.0, float(cmd.get("seconds", cmd.get("sec", 0.5)))))
                ops.sleep(sec)
                results.append({"cmd": kind, "ok": True, "seconds": sec})

            elif kind == "focus_window":
                hwnd = int(cmd.get("hwnd") or last_hwnd or 0)
                if not hwnd:
                    target_pid = cmd.get("pid") or context.get("client_pid")
                    proc_name = str(cmd.get("process_name") or context.get("client_exe") or "")
                    hwnd = ops.find_window(
                        pid=int(target_pid) if target_pid else None,
                        process_name=proc_name,
                        title_contains=str(cmd.get("title_contains") or ""),
                    )
                    if not hwnd and proc_name:
                        hwnd = ops.find_window(pid=None, process_name=proc_name)
                ok = ops.focus_window(hwnd) if hwnd else False
                results.append({"cmd": kind, "ok": ok, "hwnd": hwnd})

            elif kind == "send_hotkey":
                keys = str(cmd.get("keys", "")).strip()
                ok = ops.send_hotkey(keys) if keys else False
                results.append({"cmd": kind, "ok": ok, "keys": keys})

            elif kind == "notify":
                title = str(cmd.get("title") or "WireDeck").strip()
                msg = str(cmd.get("message") or "").strip()
                ok = ops.show_notification(title, msg) if msg else False
                results.append({"cmd": kind, "ok": ok})

            elif kind == "open_url":
                url = str(cmd.get("url") or "").strip()
                if url.lower().startswith(("https://", "http://")):
                    webbrowser.open(url)
                    results.append({"cmd": kind, "ok": True, "url": url})
                else:
                    results.append({"cmd": kind, "ok": False, "error": "invalid_url_scheme"})

            elif kind == "stop_process":
                proc_name = str(cmd.get("process_name") or "").strip()
                ok = ops.stop_process(proc_name) if proc_name else False
                results.append({"cmd": kind, "ok": ok, "process_name": proc_name})

            elif kind == "restart_process":
                exe = str(cmd.get("exe", "")).strip()
                raw_args = cmd.get("args") or []
                args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
                cwd = str(cmd.get("cwd", "")).strip()
                minimized = bool(cmd.get("minimized", False))
                if not exe:
                    results.append({"cmd": kind, "ok": False, "error": "missing_exe"})
                    continue
                pid = ops.restart_process(exe, args=args, cwd=cwd, minimized=minimized)
                results.append({"cmd": kind, "ok": True, "pid": pid, "exe": exe})

            elif kind == "keep_awake":
                enable = bool(cmd.get("enable", True))
                ok = ops.set_keep_awake(enable)
                results.append({"cmd": kind, "ok": ok, "enable": enable})

            elif kind == "set_gpu_preference":
                exe = str(cmd.get("exe") or context.get("client_exe_path") or "").strip()
                mode = str(cmd.get("mode", "high_performance")).strip()
                data_dir_raw = str(context.get("data_dir") or "").strip()
                bk_file = (Path(data_dir_raw) / "runtime" / "gpu_prefs_backup.json") if data_dir_raw else None
                ok = ops.set_gpu_preference(exe, mode, backup_file=bk_file) if exe else False
                results.append({"cmd": kind, "ok": ok, "exe": exe, "mode": mode})

            elif kind == "download_file":
                dl_url = str(cmd.get("url", "")).strip()
                fname = str(cmd.get("filename", "")).strip()
                sha = str(cmd.get("sha256", "")).strip()
                open_after = bool(cmd.get("open_after", False))
                dl_res = ops.download_file(dl_url, fname, sha256=sha, open_after=open_after)
                results.append({"cmd": kind, **dl_res})

            elif kind == "recorder_action":
                rec_act = str(cmd.get("action", "")).strip().lower()
                cb = context.get("_recorder_callback")
                if callable(cb):
                    r_out = cb(rec_act)
                    results.append({"cmd": kind, "action": rec_act, **(r_out if isinstance(r_out, dict) else {"ok": bool(r_out)})})
                else:
                    results.append({"cmd": kind, "action": rec_act, "ok": False, "error": "recorder_callback_unavailable"})
        except Exception as exc:
            results.append({"cmd": kind, "ok": False, "error": str(exc)})

    return results


def _parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
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


def _coerce_sev(raw: Any, ok_fallback: bool = True) -> Severity:
    val = str(raw or ("ok" if ok_fallback else "warn")).strip().lower()
    mapping = {
        "ok": Severity.OK,
        "warn": Severity.WARN,
        "warning": Severity.WARN,
        "fail": Severity.FAIL,
        "error": Severity.FAIL,
        "idle": Severity.IDLE,
        "skipped": Severity.SKIPPED,
    }
    return mapping.get(val, Severity.OK if ok_fallback else Severity.WARN)


def run_plugin_script(
    paths: AppPaths,
    manifest: PluginManifest,
    script_rel: str,
    *,
    event: str,
    context: dict[str, Any],
    win32_ops: Win32Ops | None = None,
) -> PluginHookResult:
    """Execute one plugin PowerShell script with JSON context and dispatch its host_commands."""
    try:
        script_path = ensure_within(manifest.plugin_dir, manifest.plugin_dir / script_rel)
    except ValueError as exc:
        return PluginHookResult(
            plugin_id=manifest.id,
            plugin_name=manifest.name,
            event=event,
            ok=False,
            status=Severity.WARN,
            code="PLUGIN_PATH_ERROR",
            message=str(exc),
        )

    cfg_state = load_plugin_config(paths, manifest)
    effective_settings = dict(cfg_state["settings"])
    overrides = context.get("plugin_overrides")
    if isinstance(overrides, dict) and isinstance(overrides.get(manifest.id), dict):
        effective_settings.update(overrides[manifest.id])
    full_ctx = {
        **context,
        "event": event,
        "plugin_id": manifest.id,
        "plugin_dir": str(manifest.plugin_dir),
        "plugin_settings": effective_settings,
        "data_dir": str(paths.data_dir),
        "wiredeck_version": APP_VERSION,
    }

    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    fd, ctx_tmp = tempfile.mkstemp(prefix=".plg_ctx_", suffix=".json", dir=str(paths.runtime_dir))
    ctx_path = Path(ctx_tmp)
    serializable_ctx = {k: v for k, v in full_ctx.items() if not str(k).startswith("_")}
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(serializable_ctx, ensure_ascii=False, indent=2))
            fh.flush()

        t0 = time.perf_counter()
        ps_res = run_ps_file(
            script_path,
            timeout=manifest.timeout_sec,
            extra_env={
                "WIREDECK_HOOK_CONTEXT_FILE": str(ctx_path),
                "WIREDECK_PLUGIN_ID": manifest.id,
                "WIREDECK_PLUGIN_DIR": str(manifest.plugin_dir),
                "WIREDECK_HOOK_EVENT": event,
                "WIREDECK_DATA_DIR": str(paths.data_dir),
            },
            cwd=manifest.plugin_dir,
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
    finally:
        try:
            ctx_path.unlink(missing_ok=True)
        except OSError:
            pass

    if ps_res.timed_out:
        return PluginHookResult(
            plugin_id=manifest.id,
            plugin_name=manifest.name,
            event=event,
            ok=False,
            status=Severity.WARN,
            code="PLUGIN_TIMEOUT",
            message=f"插件 {manifest.id} 超时（>{manifest.timeout_sec}s）",
            duration_ms=elapsed_ms,
        )

    parsed = _parse_json_stdout(ps_res.stdout)
    if parsed is None:
        return PluginHookResult(
            plugin_id=manifest.id,
            plugin_name=manifest.name,
            event=event,
            ok=ps_res.returncode == 0,
            status=Severity.OK if ps_res.returncode == 0 else Severity.WARN,
            code="PLUGIN_NO_JSON" if ps_res.returncode == 0 else "PLUGIN_ERROR",
            message=ps_res.stderr.strip()[:160] or f"脚本已返回（code={ps_res.returncode}）",
            duration_ms=elapsed_ms,
        )

    saved_patch: dict[str, Any] = {}
    if isinstance(parsed.get("save_settings"), dict) and parsed["save_settings"]:
        saved_patch = dict(parsed["save_settings"])
        save_plugin_config(paths, manifest.id, settings=saved_patch)

    cmd_results: list[dict[str, Any]] = []
    if isinstance(parsed.get("host_commands"), list) and parsed["host_commands"]:
        cmd_results = execute_host_commands(
            parsed["host_commands"],
            context=full_ctx,
            win32_ops=win32_ops,
        )

    ok_val = bool(parsed.get("ok", True))
    return PluginHookResult(
        plugin_id=manifest.id,
        plugin_name=manifest.name,
        event=event,
        ok=ok_val,
        status=_coerce_sev(parsed.get("status"), ok_val),
        code=str(parsed.get("code") or f"PLUGIN_{event.upper()}"),
        message=str(parsed.get("message") or ""),
        host_commands_executed=cmd_results,
        saved_settings=saved_patch,
        details=dict(parsed.get("details") or {}) if isinstance(parsed.get("details"), dict) else {},
        duration_ms=elapsed_ms,
    )


def emit_plugin_hook(
    paths: AppPaths,
    event: str,
    context: dict[str, Any] | None = None,
    *,
    win32_ops: Win32Ops | None = None,
) -> list[PluginHookResult]:
    """Dispatch a lifecycle hook to all enabled plugins subscribing to `event`."""
    if event not in SUPPORTED_HOOK_EVENTS:
        return []
    ctx = dict(context or {})
    results: list[PluginHookResult] = []
    for manifest in discover_plugins(paths):
        script_rel = manifest.hooks.get(event)
        if not script_rel:
            continue
        cfg_state = load_plugin_config(paths, manifest)
        if not cfg_state["enabled"]:
            continue
        try:
            res = run_plugin_script(
                paths,
                manifest,
                script_rel,
                event=event,
                context=ctx,
                win32_ops=win32_ops,
            )
            results.append(res)
        except Exception as exc:
            results.append(
                PluginHookResult(
                    plugin_id=manifest.id,
                    plugin_name=manifest.name,
                    event=event,
                    ok=False,
                    status=Severity.WARN,
                    code="PLUGIN_EXCEPTION",
                    message=str(exc),
                )
            )
    return results


def run_plugin_action(
    paths: AppPaths,
    plugin_id: str,
    action_id: str,
    context: dict[str, Any] | None = None,
    *,
    win32_ops: Win32Ops | None = None,
) -> dict[str, Any]:
    manifests = {m.id: m for m in discover_plugins(paths)}
    m = manifests.get(plugin_id)
    if m is None:
        return {"ok": False, "code": "PLUGIN_NOT_FOUND", "message": f"未找到插件：{plugin_id}"}
    act = next((a for a in m.actions if a.id == action_id), None)
    if act is None:
        return {"ok": False, "code": "ACTION_NOT_FOUND", "message": f"插件未定义操作：{action_id}"}
    res = run_plugin_script(
        paths,
        m,
        act.script,
        event=f"action:{action_id}",
        context=dict(context or {}),
        win32_ops=win32_ops,
    )
    return res.to_dict()


def run_plugin_check(
    paths: AppPaths,
    plugin_id: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifests = {m.id: m for m in discover_plugins(paths)}
    m = manifests.get(plugin_id)
    if m is None:
        return {"ok": False, "code": "PLUGIN_NOT_FOUND", "message": f"未找到插件：{plugin_id}"}
    if not m.diagnostic_check:
        return {"ok": True, "code": "NO_CHECK_DEFINED", "message": "该插件未定义额外自检脚本"}
    return run_plugin_script(
        paths,
        m,
        m.diagnostic_check,
        event="diagnostic_check",
        context=dict(context or {}),
    ).to_dict()


def resolve_plugin_dynamic_options(
    paths: AppPaths,
    plugin_id: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run any `dynamic_select` field options_script for live dropdown choices."""
    manifests = {m.id: m for m in discover_plugins(paths)}
    m = manifests.get(plugin_id)
    if m is None:
        return {"ok": False, "options_by_key": {}, "meta_by_key": {}}

    options_by_key: dict[str, list[dict[str, str]]] = {}
    meta_by_key: dict[str, Any] = {}
    cfg_state = load_plugin_config(paths, m)
    base_ctx = {
        **(context or {}),
        "event": "dynamic_options",
        "plugin_id": m.id,
        "plugin_dir": str(m.plugin_dir),
        "plugin_settings": cfg_state["settings"],
        "data_dir": str(paths.data_dir),
        "wiredeck_version": APP_VERSION,
    }

    for field in m.settings_schema:
        if field.type != "dynamic_select" or not field.options_script:
            continue
        try:
            script_path = ensure_within(m.plugin_dir, m.plugin_dir / field.options_script)
            paths.runtime_dir.mkdir(parents=True, exist_ok=True)
            fd, ctx_tmp = tempfile.mkstemp(prefix=".plg_opt_", suffix=".json", dir=str(paths.runtime_dir))
            ctx_path = Path(ctx_tmp)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(base_ctx, ensure_ascii=False, indent=2))
                    fh.flush()
                ps_res = run_ps_file(
                    script_path,
                    timeout=m.timeout_sec,
                    extra_env={
                        "WIREDECK_HOOK_CONTEXT_FILE": str(ctx_path),
                        "WIREDECK_PLUGIN_ID": m.id,
                        "WIREDECK_PLUGIN_DIR": str(m.plugin_dir),
                        "WIREDECK_DATA_DIR": str(paths.data_dir),
                    },
                    cwd=m.plugin_dir,
                )
            finally:
                try:
                    ctx_path.unlink(missing_ok=True)
                except OSError:
                    pass
            parsed = _parse_json_stdout(ps_res.stdout) if ps_res.returncode == 0 else None
            if parsed and isinstance(parsed.get("options"), list):
                opts: list[dict[str, str]] = []
                for o in parsed["options"]:
                    if isinstance(o, dict):
                        opts.append({"value": str(o.get("value", "")), "label": str(o.get("label", o.get("value", "")))})
                options_by_key[field.key] = opts
                if "meta" in parsed:
                    meta_by_key[field.key] = parsed["meta"]
        except Exception:
            continue

    return {"ok": True, "options_by_key": options_by_key, "meta_by_key": meta_by_key}


def resolve_github_plugin_url(raw_input: str) -> str:
    """Resolve `owner/repo`, `https://github.com/owner/repo`, or raw JSON URL cleanly."""
    s = (raw_input or "").strip()
    if not s:
        raise ValueError("Empty GitHub URL or repo specifier")
    if s.startswith(("https://github.com/", "http://github.com/")):
        parsed = urllib.parse.urlparse(s)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[1].endswith(".git"):
            parts[1] = parts[1][:-4]
        if len(parts) >= 2 and "/blob/" not in parsed.path:
            return f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/main/plugin-package.json"
        if len(parts) >= 5 and parts[2] == "blob":
            branch = parts[3]
            rest = "/".join(parts[4:])
            return f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/{branch}/{rest}"
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", s):
        return f"https://raw.githubusercontent.com/{s}/main/plugin-package.json"
    if s.startswith(("https://", "http://")):
        return s
    raise ValueError(f"Unsupported GitHub plugin URL format: {raw_input}")


def _fetch_json(
    url: str,
    *,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 12.0,
) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"WireDeck/{APP_VERSION}",
        },
    )
    with urlopen_fn(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Remote JSON root must be an object")
    return data


def fetch_plugin_catalog(
    registry_url: str = "",
    *,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Fetch available patches/plugins from the configured GitHub registry index."""
    url = (registry_url or "").strip() or DEFAULT_PLUGIN_REGISTRY_URL
    try:
        data = _fetch_json(url, urlopen_fn=urlopen_fn, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "registry_url": url,
            "plugins": [],
            "error": f"GitHub 仓库索引暂时无法访问（HTTP {exc.code}），可稍后重试或粘贴独立补丁 JSON 链接",
        }
    except Exception as exc:
        return {
            "ok": False,
            "registry_url": url,
            "plugins": [],
            "error": f"无法拉取 GitHub 补丁目录（{exc}）",
        }

    schema = str(data.get("schema", ""))
    if schema not in (PLUGIN_REGISTRY_SCHEMA, ""):
        return {
            "ok": False,
            "registry_url": url,
            "plugins": [],
            "error": f"不支持的补丁源格式：{schema}",
        }

    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        plugins = []
    return {
        "ok": True,
        "registry_url": url,
        "generated_at": str(data.get("generated_at", "")),
        "plugins": [p for p in plugins if isinstance(p, dict)],
        "error": "",
    }


def install_plugin_from_entry(
    paths: AppPaths,
    entry: dict[str, Any],
    *,
    source_url: str = "",
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
) -> ActionResult:
    """Download every file declared by a catalog/package entry, verify SHA-256, validate, and install."""
    plugin_id = str(entry.get("id", "")).strip()
    if not VALID_PLUGIN_ID_RE.match(plugin_id):
        return ActionResult(ok=False, code="INVALID_PLUGIN_ID", message=f"非法的插件 ID：{plugin_id}", exit_code=1)

    files = entry.get("files")
    if not isinstance(files, list) or not files:
        return ActionResult(ok=False, code="INVALID_PACKAGE", message="补丁包清单缺少 files 列表", exit_code=1)

    paths.plugins_dir.mkdir(parents=True, exist_ok=True)
    staging_parent = paths.runtime_dir / "plugin_stage"
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f"{plugin_id}_", dir=str(staging_parent)))

    try:
        for fentry in files:
            if not isinstance(fentry, dict):
                raise ValueError("Invalid file entry in plugin package")
            rel = str(fentry.get("path", "")).strip()
            dl_url = str(fentry.get("url", "")).strip()
            sha = str(fentry.get("sha256", "")).strip()
            if not rel or Path(rel).is_absolute():
                raise ValueError(f"Rejecting unsafe relative path: {rel!r}")
            dest = ensure_within(staging_dir, staging_dir / rel)
            if not dl_url.startswith(("https://", "http://")):
                raise ValueError(f"Unsupported download URL for {rel}: {dl_url}")
            download_and_verify_asset(dl_url, dest, sha, urlopen_fn=urlopen_fn, timeout=20.0)

        manifest, errors = validate_plugin_dir(staging_dir)
        if manifest is None or errors:
            raise ValueError("; ".join(errors or ["Plugin manifest validation failed"]))
        if manifest.id != plugin_id:
            raise ValueError(f"Manifest id '{manifest.id}' does not match package id '{plugin_id}'")

        install_meta = {
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "source_url": source_url or str(entry.get("homepage", "")),
            "version": manifest.version,
            "verified_sha256": True,
        }
        _atomic_write_json(staging_dir / "_install_meta.json", install_meta)

        target_dir = ensure_within(paths.plugins_dir, paths.plugins_dir / plugin_id)
        old_backup: Path | None = None
        if target_dir.exists():
            old_backup = paths.plugins_dir / f".old_{plugin_id}_{int(time.time())}"
            os.replace(target_dir, old_backup)
        try:
            os.replace(staging_dir, target_dir)
        except Exception:
            if old_backup and old_backup.exists() and not target_dir.exists():
                os.replace(old_backup, target_dir)
            raise
        if old_backup and old_backup.exists():
            shutil.rmtree(old_backup, ignore_errors=True)

        # Ensure config entry exists preserving user's previous settings if updating
        cfg_file = _plugin_config_path(paths, plugin_id)
        if not cfg_file.is_file():
            save_plugin_config(
                paths,
                plugin_id,
                enabled=manifest.enabled_by_default,
                settings=_default_settings_from_manifest(manifest),
            )
        else:
            merged = load_plugin_config(paths, manifest)
            save_plugin_config(paths, plugin_id, enabled=merged["enabled"], settings=merged["settings"])

        return ActionResult(
            ok=True,
            code="PLUGIN_INSTALLED",
            message=f"已从 GitHub 校验并安装补丁：{manifest.name} v{manifest.version}",
            details={"plugin_id": plugin_id, "version": manifest.version, "dir": str(target_dir)},
        )
    except Exception as exc:
        return ActionResult(
            ok=False,
            code="PLUGIN_INSTALL_FAILED",
            message=f"安装补丁 {plugin_id} 失败：{exc}",
            exit_code=1,
        )
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def install_plugin_from_github(
    paths: AppPaths,
    *,
    plugin_id: str = "",
    registry_url: str = "",
    direct_url: str = "",
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
) -> ActionResult:
    """Install/update a plugin either by id from the catalog or from a direct GitHub repo/JSON URL."""
    if direct_url.strip():
        try:
            pkg_url = resolve_github_plugin_url(direct_url)
            entry = _fetch_json(pkg_url, urlopen_fn=urlopen_fn, timeout=15.0)
        except Exception as exc:
            return ActionResult(ok=False, code="GITHUB_FETCH_FAILED", message=f"读取 GitHub 补丁描述失败：{exc}", exit_code=1)
        return install_plugin_from_entry(paths, entry, source_url=pkg_url, urlopen_fn=urlopen_fn)

    cat = fetch_plugin_catalog(registry_url, urlopen_fn=urlopen_fn, timeout=15.0)
    if not cat["ok"]:
        return ActionResult(ok=False, code="CATALOG_FETCH_FAILED", message=str(cat["error"]), exit_code=1)
    target = next((p for p in cat["plugins"] if str(p.get("id", "")).strip() == plugin_id.strip()), None)
    if target is None:
        return ActionResult(ok=False, code="PLUGIN_NOT_IN_CATALOG", message=f"GitHub 目录中未找到补丁：{plugin_id}", exit_code=1)
    return install_plugin_from_entry(paths, target, source_url=str(cat["registry_url"]), urlopen_fn=urlopen_fn)


def uninstall_plugin(paths: AppPaths, plugin_id: str, *, remove_config: bool = False) -> ActionResult:
    """Uninstall an installed plugin directory cleanly."""
    if not VALID_PLUGIN_ID_RE.match(plugin_id or ""):
        return ActionResult(ok=False, code="INVALID_PLUGIN_ID", message=f"非法的插件 ID：{plugin_id}", exit_code=1)
    try:
        target_dir = ensure_within(paths.plugins_dir, paths.plugins_dir / plugin_id)
    except ValueError as exc:
        return ActionResult(ok=False, code="INVALID_PLUGIN_ID", message=str(exc), exit_code=1)
    if not target_dir.is_dir():
        return ActionResult(ok=False, code="PLUGIN_NOT_FOUND", message=f"未安装插件：{plugin_id}", exit_code=1)
    shutil.rmtree(target_dir, ignore_errors=False)
    if remove_config:
        _plugin_config_path(paths, plugin_id).unlink(missing_ok=True)
    return ActionResult(
        ok=True,
        code="PLUGIN_UNINSTALLED",
        message=f"已卸载插件：{plugin_id}",
        details={"plugin_id": plugin_id, "removed_config": remove_config},
    )
