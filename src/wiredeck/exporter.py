"""Privacy-sanitized diagnostic bundle exporter and DSH/AI handoff prompt generator."""

from __future__ import annotations

import getpass
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from wiredeck.config import AppConfig
from wiredeck.models import DiagnosticReport
from wiredeck.paths import AppPaths
from wiredeck.version import APP_VERSION, EXT_SCHEMA_VERSION, SCHEMA_VERSION

SECRET_KEY_RE = re.compile(
    r"(token|password|passwd|secret|api_?key|private_?key|secret_?key|credential|cookie|passphrase|bearer)",
    re.IGNORECASE,
)
BEARER_VALUE_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._-]{12,}", re.IGNORECASE)


def _redact_string(text: str, username: str) -> str:
    out = BEARER_VALUE_RE.sub(r"\1<REDACTED>", text)
    if username:
        escaped = re.escape(username)
        # Replace C:\Users\<username> or c:/users/<username>
        out = re.sub(
            rf"([A-Za-z]:[\\/]+Users[\\/]+){escaped}(?=[\\/\"'\s,;]|$)",
            r"\1<USER>",
            out,
            flags=re.IGNORECASE,
        )
        # Replace any remaining path segments containing the username
        out = re.sub(
            rf"([\\/]){escaped}(?=[\\/\"'\s,;]|$)",
            r"\1<USER>",
            out,
            flags=re.IGNORECASE,
        )
    return out


def sanitize_payload(obj: Any, username: str | None = None) -> Any:
    """Recursively strip secrets and replace Windows user profile names with <USER>."""
    user = username if username is not None else (os.environ.get("USERNAME") or getpass.getuser() or "")
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for k, v in obj.items():
            key_str = str(k)
            if SECRET_KEY_RE.search(key_str):
                cleaned[key_str] = "<REDACTED>"
            else:
                cleaned[key_str] = sanitize_payload(v, user)
        return cleaned
    if isinstance(obj, list):
        return [sanitize_payload(item, user) for item in obj]
    if isinstance(obj, str):
        return _redact_string(obj, user)
    return obj


def _collect_recent_logs(paths: AppPaths, max_files: int = 2, max_lines: int = 60) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    if not paths.logs_dir.is_dir():
        return logs
    files = sorted(paths.logs_dir.glob("wiredeck_*.log"))[-max_files:]
    for lf in files:
        try:
            lines = lf.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            logs.append({"file": lf.name, "tail": lines[-max_lines:]})
        except OSError:
            continue
    return logs


def build_dsh_handoff_prompt(
    bundle_path: Path,
    paths: AppPaths,
    report: DiagnosticReport,
) -> str:
    """Generate a concise, copy-ready prompt for DSH or any local AI assistant."""
    cli_path = paths.exe_dir / "wiredeck-cli.exe"
    cli_hint = str(cli_path) if cli_path.is_file() else "wiredeck-cli.exe"
    fault_line = (
        f"{report.primary_fault.layer} / {report.primary_fault.code}（{report.primary_fault.message}）"
        if report.primary_fault
        else "无明显硬阻断（可结合诊断包深入排查）"
    )
    return (
        "请帮我排查笔记本与游戏机有线直连问题（WireDeck 现场交接）：\n"
        f"- 当前状态：{report.mode} | 主断点：{fault_line}\n"
        f"- 脱敏诊断包：{bundle_path}\n"
        f"- CLI 接口：`{cli_hint} diagnose --json` / `apply <action> --json` / `ext validate <dir>`\n"
        f"- 热加载扩展目录：`{paths.extensions_dir}`（协议 `{EXT_SCHEMA_VERSION}`，写入 `ext.json` + `check.ps1` / `fix.ps1` 即可自动融入 WireDeck 主诊断界面）\n"
        "请先读取上述诊断包定位根因；若属于 WireDeck 尚未内置的问题，可直接往扩展目录写入检测/修复插件并验证。"
    )


def export_diagnostic_bundle(
    paths: AppPaths,
    report: DiagnosticReport,
    cfg: AppConfig,
    out_path: Path | None = None,
    *,
    username: str | None = None,
) -> tuple[Path, str]:
    """Export sanitized diagnostic JSON bundle and return (bundle_path, dsh_prompt)."""
    target = Path(out_path) if out_path is not None else (paths.exports_dir / "diagnostic_latest.json")
    target.parent.mkdir(parents=True, exist_ok=True)

    raw_bundle = {
        "schema_version": SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": cfg.to_dict(),
        "report": report.to_dict(),
        "recent_logs": _collect_recent_logs(paths),
    }
    sanitized = sanitize_payload(raw_bundle, username=username)
    target.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    prompt = build_dsh_handoff_prompt(target, paths, report)
    return target, prompt
