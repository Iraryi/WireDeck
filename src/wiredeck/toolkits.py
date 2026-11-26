"""Bundled read-only universal and per-preset AI/DSH toolkit runner."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from wiredeck.config import AppConfig
from wiredeck.paths import ensure_within, get_toolkits_dir
from wiredeck.presets import normalize_preset_id
from wiredeck.ps_runner import run_ps_file


@dataclass(frozen=True)
class ToolkitItem:
    id: str
    scope: str  # "common" or preset id
    script: str
    name_zh: str
    name_en: str
    description_zh: str
    description_en: str
    timeout_sec: int = 12
    read_only: bool = True
    script_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolkitRunResult:
    ok: bool
    tool_id: str
    scope: str
    duration_ms: int
    data: dict[str, Any] = field(default_factory=dict)
    raw_output: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_manifest(root: Path | None = None) -> list[ToolkitItem]:
    base = (root or get_toolkits_dir()).resolve()
    manifest_file = base / "manifest.json"
    if not manifest_file.is_file():
        return []
    raw = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    items: list[ToolkitItem] = []
    for entry in raw.get("tools") or []:
        rel = str(entry.get("script") or "")
        if not rel:
            continue
        try:
            resolved = ensure_within(base, base / rel)
        except ValueError:
            continue
        if not resolved.is_file():
            continue
        items.append(
            ToolkitItem(
                id=str(entry["id"]),
                scope=str(entry.get("scope", "common")),
                script=rel,
                name_zh=str(entry.get("name_zh", entry["id"])),
                name_en=str(entry.get("name_en", entry["id"])),
                description_zh=str(entry.get("description_zh", "")),
                description_en=str(entry.get("description_en", "")),
                timeout_sec=max(2, min(20, int(entry.get("timeout_sec", 12)))),
                read_only=bool(entry.get("read_only", True)),
                script_path=str(resolved),
            )
        )
    return items


def discover_toolkits(
    *,
    preset_id: str | None = None,
    include_common: bool = True,
    root: Path | None = None,
) -> list[ToolkitItem]:
    """Return bundled tools matching the requested preset and/or universal common scope."""
    all_items = _load_manifest(root)
    if preset_id is None and include_common:
        return [t for t in all_items if t.scope == "common"]

    norm = normalize_preset_id(preset_id) if preset_id is not None else None
    out: list[ToolkitItem] = []
    for item in all_items:
        if norm is not None and item.scope == norm:
            out.append(item)
        elif include_common and item.scope == "common":
            out.append(item)
    return out


def discover_all_toolkits(root: Path | None = None) -> list[ToolkitItem]:
    """Return every bundled toolkit across common + all presets."""
    return _load_manifest(root)


def _parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    for cand in (text, text[text.find("{") :] if "{" in text else ""):
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def run_toolkit(
    tool_id: str,
    *,
    cfg: AppConfig | None = None,
    root: Path | None = None,
) -> ToolkitRunResult:
    """Execute a bundled read-only toolkit script safely and return parsed JSON."""
    items = {t.id: t for t in _load_manifest(root)}
    tool = items.get(tool_id)
    if tool is None:
        return ToolkitRunResult(
            ok=False,
            tool_id=tool_id,
            scope="",
            duration_ms=0,
            error=f"Toolkit not found: {tool_id}",
        )

    active_cfg = cfg or AppConfig()
    base = (root or get_toolkits_dir()).resolve()
    try:
        script_path = ensure_within(base, base / tool.script)
    except ValueError as exc:
        return ToolkitRunResult(
            ok=False,
            tool_id=tool.id,
            scope=tool.scope,
            duration_ms=0,
            error=str(exc),
        )

    env = {
        "WIREDECK_ACTIVE_PRESET": active_cfg.active_preset,
        "WIREDECK_TRANSPORT": active_cfg.active_transport,
        "WIREDECK_HOST_IP": active_cfg.host_ip,
        "WIREDECK_PEER_IP": active_cfg.peer_ip,
        "WIREDECK_SUBNET_CIDR": active_cfg.subnet_cidr,
        "WIREDECK_SUBNET_MASK": active_cfg.subnet_mask,
        "WIREDECK_DNS_SERVERS": ",".join(active_cfg.dns_servers),
        "WIREDECK_ETH_INTERFACE": active_cfg.ethernet_interface,
        "WIREDECK_WIFI_INTERFACE": active_cfg.wifi_interface,
        "WIREDECK_WIRELESS_PEER_IP": active_cfg.wireless_peer_ip,
    }
    t0 = time.perf_counter()
    ps_res = run_ps_file(script_path, timeout=tool.timeout_sec, extra_env=env, cwd=script_path.parent)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if ps_res.timed_out:
        return ToolkitRunResult(
            ok=False,
            tool_id=tool.id,
            scope=tool.scope,
            duration_ms=elapsed_ms,
            error=f"Toolkit timed out after {tool.timeout_sec}s",
        )

    parsed = _parse_json_stdout(ps_res.stdout)
    if parsed is None:
        return ToolkitRunResult(
            ok=False,
            tool_id=tool.id,
            scope=tool.scope,
            duration_ms=elapsed_ms,
            raw_output=ps_res.stdout[:800],
            error=ps_res.stderr.strip()[:400] or "Toolkit did not return valid JSON",
        )

    return ToolkitRunResult(
        ok=(ps_res.returncode == 0),
        tool_id=tool.id,
        scope=tool.scope,
        duration_ms=elapsed_ms,
        data=parsed,
        raw_output=ps_res.stdout[:800] if ps_res.returncode != 0 else "",
        error=ps_res.stderr.strip()[:400] if ps_res.returncode != 0 else "",
    )
