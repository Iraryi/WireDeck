"""Path resolution for standard and portable modes with traversal guards."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from wiredeck.version import APP_NAME


@dataclass(frozen=True)
class AppPaths:
    exe_dir: Path
    is_portable: bool
    data_dir: Path
    config_file: Path
    extensions_dir: Path
    plugins_dir: Path
    plugin_configs_dir: Path
    logs_dir: Path
    exports_dir: Path
    runtime_dir: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "exe_dir": str(self.exe_dir),
            "is_portable": self.is_portable,
            "data_dir": str(self.data_dir),
            "config_file": str(self.config_file),
            "extensions_dir": str(self.extensions_dir),
            "plugins_dir": str(self.plugins_dir),
            "plugin_configs_dir": str(self.plugin_configs_dir),
            "logs_dir": str(self.logs_dir),
            "exports_dir": str(self.exports_dir),
            "runtime_dir": str(self.runtime_dir),
        }


def get_exe_dir() -> Path:
    """Return directory containing the running executable (or repo root in dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def get_cli_executable(exe_dir: Path | None = None) -> Path | None:
    """Return path to wiredeck-cli.exe if packaged alongside the running binary."""
    base = (exe_dir or get_exe_dir()).resolve()
    candidate = base / "wiredeck-cli.exe"
    if candidate.is_file():
        return candidate
    return None


def get_web_dir() -> Path:
    """Return directory containing web UI assets in both dev and frozen builds."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "wiredeck" / "web"
        if bundled.is_dir():
            return bundled
        bundled_alt = Path(meipass) / "web"
        if bundled_alt.is_dir():
            return bundled_alt
    return Path(__file__).resolve().parent / "web"


def get_toolkits_dir() -> Path:
    """Return directory containing bundled read-only AI/DSH toolkits in dev and frozen builds."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "wiredeck" / "toolkits"
        if bundled.is_dir():
            return bundled
        bundled_alt = Path(meipass) / "toolkits"
        if bundled_alt.is_dir():
            return bundled_alt
    return Path(__file__).resolve().parent / "toolkits"


def get_assets_dir() -> Path:
    """Return directory containing brand icons and static assets."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "assets"
        if bundled.is_dir():
            return bundled
    exe_assets = get_exe_dir() / "assets"
    if exe_assets.is_dir():
        return exe_assets
    return Path(__file__).resolve().parents[2] / "assets"


def ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve candidate relative to root and raise ValueError if it escapes root."""
    root_resolved = Path(root).resolve(strict=False)
    cand_path = Path(candidate)
    if not cand_path.is_absolute():
        cand_path = root_resolved / cand_path
    cand_resolved = cand_path.resolve(strict=False)
    try:
        cand_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(
            f"Path {cand_resolved!s} escapes allowed root {root_resolved!s}"
        ) from exc
    return cand_resolved


def resolve_app_paths_from_data_dir(
    data_dir: Path,
    exe_dir: Path | None = None,
    *,
    create: bool = True,
) -> AppPaths:
    """Build AppPaths pinned to an explicit data_dir (used by elevated --worker)."""
    resolved_exe_dir = Path(exe_dir).resolve() if exe_dir is not None else get_exe_dir()
    resolved_data = Path(data_dir).resolve()
    is_portable = (resolved_exe_dir / "portable.mode").is_file()
    paths = AppPaths(
        exe_dir=resolved_exe_dir,
        is_portable=is_portable,
        data_dir=resolved_data,
        config_file=resolved_data / "config.json",
        extensions_dir=resolved_data / "extensions",
        plugins_dir=resolved_data / "plugins",
        plugin_configs_dir=resolved_data / "plugin_configs",
        logs_dir=resolved_data / "logs",
        exports_dir=resolved_data / "exports",
        runtime_dir=resolved_data / "runtime",
    )
    if create:
        for d in (
            paths.data_dir,
            paths.extensions_dir,
            paths.plugins_dir,
            paths.plugin_configs_dir,
            paths.logs_dir,
            paths.exports_dir,
            paths.runtime_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
    return paths


def resolve_app_paths(
    exe_dir: Path | None = None,
    local_appdata: Path | None = None,
    *,
    create: bool = True,
) -> AppPaths:
    """Resolve WireDeck data layout based on explicit `portable.mode` marker."""
    resolved_exe_dir = Path(exe_dir).resolve() if exe_dir is not None else get_exe_dir()
    is_portable = (resolved_exe_dir / "portable.mode").is_file()

    if is_portable:
        data_dir = (resolved_exe_dir / "data").resolve()
    else:
        if local_appdata is not None:
            base_local = Path(local_appdata)
        else:
            env_local = os.environ.get("LOCALAPPDATA")
            base_local = Path(env_local) if env_local else (Path.home() / "AppData" / "Local")
        data_dir = (base_local / APP_NAME).resolve()

    paths = AppPaths(
        exe_dir=resolved_exe_dir,
        is_portable=is_portable,
        data_dir=data_dir,
        config_file=data_dir / "config.json",
        extensions_dir=data_dir / "extensions",
        plugins_dir=data_dir / "plugins",
        plugin_configs_dir=data_dir / "plugin_configs",
        logs_dir=data_dir / "logs",
        exports_dir=data_dir / "exports",
        runtime_dir=data_dir / "runtime",
    )

    if create:
        for d in (
            paths.data_dir,
            paths.extensions_dir,
            paths.plugins_dir,
            paths.plugin_configs_dir,
            paths.logs_dir,
            paths.exports_dir,
            paths.runtime_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    return paths
