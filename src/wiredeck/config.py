"""Typed application configuration with shared host fields & isolated per-preset settings."""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from wiredeck.paths import AppPaths
from wiredeck.presets import ALL_PRESET_IDS, get_preset, normalize_preset_id

VALID_PROFILES = ALL_PRESET_IDS
VALID_LANGUAGES = ("zh-CN", "zh-TW", "en", "ja")
VALID_THEMES = ("light", "dark")


@dataclass
class PresetSettings:
    host_ip: str = "192.168.50.2"
    prefix_length: int = 24
    peer_ip: str = "192.168.50.1"
    dns_servers: list[str] = field(default_factory=lambda: ["223.5.5.5", "8.8.8.8"])
    wizard_completed: bool = False
    preferred_client: str = "auto"
    custom_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_preset_settings(preset_id: str) -> PresetSettings:
    p = get_preset(preset_id)
    return PresetSettings(
        host_ip=p.default_host_ip,
        prefix_length=p.default_prefix_length,
        peer_ip=p.default_peer_ip,
        dns_servers=list(p.default_dns_servers),
        wizard_completed=False,
        preferred_client="auto",
        custom_notes="",
    )


def _default_presets_map() -> dict[str, PresetSettings]:
    return {pid: default_preset_settings(pid) for pid in ALL_PRESET_IDS}


class AppConfig:
    """Shared host environment settings + isolated per-preset device settings."""

    def __init__(
        self,
        *,
        language: str = "zh-CN",
        theme: str = "light",
        active_preset: str | None = None,
        console_profile: str | None = None,
        ethernet_interface: str = "",
        uplink_interface: str = "",
        http_bridge_enabled: bool = False,
        onboarding_completed: bool = False,
        presets: dict[str, PresetSettings] | None = None,
        # Optional direct overrides for the active preset (clean ergonomics & legacy compat)
        host_ip: str | None = None,
        prefix_length: int | None = None,
        peer_ip: str | None = None,
        dns_servers: list[str] | None = None,
        wizard_completed: bool | None = None,
    ) -> None:
        self.language = language if language in VALID_LANGUAGES else "zh-CN"
        self.theme = theme if theme in VALID_THEMES else "light"
        chosen = active_preset if active_preset is not None else (console_profile or "xbox")
        self.active_preset = normalize_preset_id(chosen)
        self.ethernet_interface = ethernet_interface
        self.uplink_interface = uplink_interface
        self.http_bridge_enabled = bool(http_bridge_enabled)
        self.onboarding_completed = bool(onboarding_completed)

        self.presets: dict[str, PresetSettings] = _default_presets_map()
        if presets:
            for k, v in presets.items():
                nk = normalize_preset_id(k)
                if isinstance(v, PresetSettings):
                    self.presets[nk] = v

        active = self.current_preset_settings
        if host_ip is not None:
            active.host_ip = _valid_ipv4(host_ip, active.host_ip)
        if prefix_length is not None:
            active.prefix_length = _valid_prefix(prefix_length, active.prefix_length)
        if peer_ip is not None:
            active.peer_ip = _valid_ipv4(peer_ip, active.peer_ip)
        if dns_servers is not None:
            active.dns_servers = _valid_dns_list(dns_servers, active.dns_servers)
        if wizard_completed is not None:
            active.wizard_completed = bool(wizard_completed)

    @property
    def current_preset_settings(self) -> PresetSettings:
        if self.active_preset not in self.presets:
            self.presets[self.active_preset] = default_preset_settings(self.active_preset)
        return self.presets[self.active_preset]

    def switch_preset(self, preset_id: str) -> PresetSettings:
        """Switch active preset while preserving all other presets' isolated settings."""
        self.active_preset = normalize_preset_id(preset_id)
        return self.current_preset_settings

    @property
    def console_profile(self) -> str:
        return self.active_preset

    @console_profile.setter
    def console_profile(self, value: str) -> None:
        self.active_preset = normalize_preset_id(value)

    @property
    def host_ip(self) -> str:
        return self.current_preset_settings.host_ip

    @host_ip.setter
    def host_ip(self, value: str) -> None:
        self.current_preset_settings.host_ip = _valid_ipv4(value, self.current_preset_settings.host_ip)

    @property
    def prefix_length(self) -> int:
        return self.current_preset_settings.prefix_length

    @prefix_length.setter
    def prefix_length(self, value: int) -> None:
        self.current_preset_settings.prefix_length = _valid_prefix(
            value, self.current_preset_settings.prefix_length
        )

    @property
    def peer_ip(self) -> str:
        return self.current_preset_settings.peer_ip

    @peer_ip.setter
    def peer_ip(self, value: str) -> None:
        self.current_preset_settings.peer_ip = _valid_ipv4(value, self.current_preset_settings.peer_ip)

    @property
    def dns_servers(self) -> list[str]:
        return self.current_preset_settings.dns_servers

    @dns_servers.setter
    def dns_servers(self, value: list[str]) -> None:
        self.current_preset_settings.dns_servers = _valid_dns_list(
            value, self.current_preset_settings.dns_servers
        )

    @property
    def wizard_completed(self) -> bool:
        return self.current_preset_settings.wizard_completed

    @wizard_completed.setter
    def wizard_completed(self, value: bool) -> None:
        self.current_preset_settings.wizard_completed = bool(value)

    @property
    def subnet_cidr(self) -> str:
        net = ipaddress.IPv4Interface(f"{self.host_ip}/{self.prefix_length}").network
        return str(net)

    @property
    def subnet_mask(self) -> str:
        net = ipaddress.IPv4Interface(f"{self.host_ip}/{self.prefix_length}").network
        return str(net.netmask)

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "theme": self.theme,
            "active_preset": self.active_preset,
            "console_profile": self.active_preset,
            "ethernet_interface": self.ethernet_interface,
            "uplink_interface": self.uplink_interface,
            "http_bridge_enabled": self.http_bridge_enabled,
            "onboarding_completed": self.onboarding_completed,
            # Flattened active-preset convenience fields for GUI/CLI/exporter consumers
            "host_ip": self.host_ip,
            "prefix_length": self.prefix_length,
            "peer_ip": self.peer_ip,
            "dns_servers": list(self.dns_servers),
            "wizard_completed": self.wizard_completed,
            "subnet_cidr": self.subnet_cidr,
            "subnet_mask": self.subnet_mask,
            # Full isolated per-preset store
            "presets": {pid: self.presets[pid].to_dict() for pid in ALL_PRESET_IDS},
        }


def _valid_ipv4(value: Any, default: str) -> str:
    if not isinstance(value, str):
        return default
    try:
        ipaddress.IPv4Address(value.strip())
        return value.strip()
    except ValueError:
        return default


def _valid_prefix(value: Any, default: int) -> int:
    try:
        n = int(value)
        return n if 8 <= n <= 30 else default
    except (TypeError, ValueError):
        return default


def _valid_dns_list(value: Any, default: list[str]) -> list[str]:
    dns_servers: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                try:
                    ipaddress.IPv4Address(item.strip())
                    dns_servers.append(item.strip())
                except ValueError:
                    continue
    return dns_servers if dns_servers else list(default)


def _normalize_preset_settings(preset_id: str, raw: Any) -> PresetSettings:
    base = default_preset_settings(preset_id)
    if not isinstance(raw, dict):
        return base
    return PresetSettings(
        host_ip=_valid_ipv4(raw.get("host_ip"), base.host_ip),
        prefix_length=_valid_prefix(raw.get("prefix_length"), base.prefix_length),
        peer_ip=_valid_ipv4(raw.get("peer_ip"), base.peer_ip),
        dns_servers=_valid_dns_list(raw.get("dns_servers"), base.dns_servers),
        wizard_completed=bool(raw.get("wizard_completed", base.wizard_completed)),
        preferred_client=str(raw.get("preferred_client", "auto")) if isinstance(raw.get("preferred_client"), str) else "auto",
        custom_notes=str(raw.get("custom_notes", "")) if isinstance(raw.get("custom_notes"), str) else "",
    )


def _normalize_config(raw: dict[str, Any]) -> AppConfig:
    language = raw.get("language", "zh-CN")
    if language not in VALID_LANGUAGES:
        language = "zh-CN"

    theme = raw.get("theme", "light")
    if theme not in VALID_THEMES:
        theme = "light"

    raw_preset = raw.get("active_preset", raw.get("console_profile", "xbox"))
    active_preset = normalize_preset_id(raw_preset)

    presets_map = _default_presets_map()
    raw_presets = raw.get("presets")
    if isinstance(raw_presets, dict):
        for k, v in raw_presets.items():
            nk = normalize_preset_id(k)
            presets_map[nk] = _normalize_preset_settings(nk, v)

    # Support flat overrides (both legacy v0.1.0 flat config.json AND partial GUI save_settings patches)
    active_ps = presets_map[active_preset]
    if "host_ip" in raw:
        active_ps.host_ip = _valid_ipv4(raw.get("host_ip"), active_ps.host_ip)
    if "prefix_length" in raw:
        active_ps.prefix_length = _valid_prefix(raw.get("prefix_length"), active_ps.prefix_length)
    if "peer_ip" in raw:
        active_ps.peer_ip = _valid_ipv4(raw.get("peer_ip"), active_ps.peer_ip)
    if "dns_servers" in raw:
        active_ps.dns_servers = _valid_dns_list(raw.get("dns_servers"), active_ps.dns_servers)
    if "wizard_completed" in raw:
        active_ps.wizard_completed = bool(raw.get("wizard_completed"))

    eth = raw.get("ethernet_interface", "")
    uplink = raw.get("uplink_interface", "")

    return AppConfig(
        language=language,
        theme=theme,
        active_preset=active_preset,
        ethernet_interface=str(eth) if isinstance(eth, str) else "",
        uplink_interface=str(uplink) if isinstance(uplink, str) else "",
        http_bridge_enabled=bool(raw.get("http_bridge_enabled", False)),
        onboarding_completed=bool(raw.get("onboarding_completed", False)),
        presets=presets_map,
    )


def load_config(paths: AppPaths) -> AppConfig:
    """Load configuration, backing up corrupt JSON files rather than crashing."""
    cfg_path = paths.config_file
    if not cfg_path.is_file():
        return AppConfig()

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("config root must be a JSON object")
        return _normalize_config(raw)
    except Exception:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup = paths.data_dir / f"config.json.corrupt.{stamp}"
        try:
            shutil.copy2(cfg_path, backup)
        except OSError:
            pass
        return AppConfig()


def save_config(paths: AppPaths, cfg: AppConfig) -> None:
    """Atomically write UTF-8 config.json with isolated per-preset sub-objects."""
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "wiredeck-config/v2",
        "language": cfg.language,
        "theme": cfg.theme,
        "active_preset": cfg.active_preset,
        "ethernet_interface": cfg.ethernet_interface,
        "uplink_interface": cfg.uplink_interface,
        "http_bridge_enabled": cfg.http_bridge_enabled,
        "onboarding_completed": cfg.onboarding_completed,
        "presets": {pid: cfg.presets[pid].to_dict() for pid in ALL_PRESET_IDS},
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".config_", suffix=".tmp", dir=str(paths.data_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, paths.config_file)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
