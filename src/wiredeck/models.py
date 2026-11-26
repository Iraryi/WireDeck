"""Typed domain models for diagnostics, actions, and DSH/AI extensions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Severity(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    IDLE = "idle"
    SKIPPED = "skipped"


SEVERITY_RANK: dict[Severity, int] = {
    Severity.SKIPPED: 0,
    Severity.IDLE: 1,
    Severity.OK: 2,
    Severity.WARN: 3,
    Severity.FAIL: 4,
}


@dataclass
class FaultItem:
    code: str
    layer: str
    severity: Severity
    title: str
    message: str
    source: str = "builtin"
    fix_action: str | None = None
    ext_id: str | None = None
    overridden_from: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "layer": self.layer,
            "severity": self.severity.value,
            "title": self.title,
            "message": self.message,
            "source": self.source,
            "fix_action": self.fix_action,
            "ext_id": self.ext_id,
            "overridden_from": list(self.overridden_from),
            "details": dict(self.details),
        }


@dataclass
class LayerReport:
    layer: str
    name: str
    status: Severity
    summary: str
    faults: list[FaultItem] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
            "faults": [f.to_dict() for f in self.faults],
            "metrics": dict(self.metrics),
        }


@dataclass
class DiagnosticReport:
    schema_version: str
    app_version: str
    timestamp: str
    console_profile: str
    mode: str
    can_enable_direct: bool
    headline: str
    primary_fault: FaultItem | None
    layers: list[LayerReport]
    transport: str = "wired"
    snapshot: dict[str, Any] = field(default_factory=dict)
    extensions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "timestamp": self.timestamp,
            "transport": self.transport,
            "console_profile": self.console_profile,
            "mode": self.mode,
            "can_enable_direct": self.can_enable_direct,
            "headline": self.headline,
            "primary_fault": self.primary_fault.to_dict() if self.primary_fault else None,
            "layers": [layer.to_dict() for layer in self.layers],
            "snapshot": dict(self.snapshot),
            "extensions": list(self.extensions),
        }


@dataclass
class ActionResult:
    ok: bool
    code: str
    message: str
    exit_code: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    log_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "exit_code": self.exit_code,
            "details": dict(self.details),
            "log_file": self.log_file,
        }


@dataclass
class ExtensionManifest:
    schema: str
    id: str
    name: str
    author: str
    enabled: bool
    auto_check: bool
    layer: str
    overrides_faults: list[str]
    requires_admin: bool
    timeout_sec: int
    check_script: str | None
    fix_script: str | None
    description: str
    ext_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "id": self.id,
            "name": self.name,
            "author": self.author,
            "enabled": self.enabled,
            "auto_check": self.auto_check,
            "layer": self.layer,
            "overrides_faults": list(self.overrides_faults),
            "requires_admin": self.requires_admin,
            "timeout_sec": self.timeout_sec,
            "check_script": self.check_script,
            "fix_script": self.fix_script,
            "description": self.description,
            "ext_dir": str(self.ext_dir),
        }


@dataclass
class ExtensionRunResult:
    ext_id: str
    name: str
    author: str
    layer: str
    status: Severity
    code: str
    title: str
    message: str
    fix_available: bool = False
    requires_admin: bool = False
    overrides_faults: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ext_id": self.ext_id,
            "name": self.name,
            "author": self.author,
            "layer": self.layer,
            "status": self.status.value,
            "code": self.code,
            "title": self.title,
            "message": self.message,
            "fix_available": self.fix_available,
            "requires_admin": self.requires_admin,
            "overrides_faults": list(self.overrides_faults),
            "details": dict(self.details),
            "duration_ms": self.duration_ms,
        }
