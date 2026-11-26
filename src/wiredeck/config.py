"""Typed application configuration with atomic UTF-8 persistence and recovery."""

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

VALID_PROFILES = ("xbox", "ps5", "generic")
VALID_LANGUAGES = ("zh-CN", "en")


@dataclass
class AppConfig:
    language: str = "zh-CN"
    console_profile: str = "xbox"
    host_ip: str = "192.168.50.2"
    prefix_length: int = 24
    peer_ip: str = "192.168.50.1"
    dns_servers: list[str] = field(default_factory=lambda: ["223.5.5.5", "8.8.8.8"])
    ethernet_interface: str = ""
    uplink_interface: str = ""
    http_bridge_enabled: bool = False
    wizard_completed: bool = False

    @property
    def subnet_cidr(self) -> str:
        net = ipaddress.IPv4Interface(f"{self.host_ip}/{self.prefix_length}").network
        return str(net)

    @property
    def subnet_mask(self) -> str:
        net = ipaddress.IPv4Interface(f"{self.host_ip}/{self.prefix_length}").network
        return str(net.netmask)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["subnet_cidr"] = self.subnet_cidr
        data["subnet_mask"] = self.subnet_mask
        return data


def _valid_ipv4(value: Any, default: str) -> str:
    if not isinstance(value, str):
        return default
    try:
        ipaddress.IPv4Address(value.strip())
        return value.strip()
    except ValueError:
        return default


def _normalize_config(raw: dict[str, Any]) -> AppConfig:
    defaults = AppConfig()
    language = raw.get("language", defaults.language)
    if language not in VALID_LANGUAGES:
        language = defaults.language

    profile = raw.get("console_profile", defaults.console_profile)
    if profile not in VALID_PROFILES:
        profile = defaults.console_profile

    host_ip = _valid_ipv4(raw.get("host_ip"), defaults.host_ip)
    peer_ip = _valid_ipv4(raw.get("peer_ip"), defaults.peer_ip)

    prefix_raw = raw.get("prefix_length", defaults.prefix_length)
    try:
        prefix_length = int(prefix_raw)
        if not (8 <= prefix_length <= 30):
            prefix_length = defaults.prefix_length
    except (TypeError, ValueError):
        prefix_length = defaults.prefix_length

    dns_raw = raw.get("dns_servers", defaults.dns_servers)
    dns_servers: list[str] = []
    if isinstance(dns_raw, list):
        for item in dns_raw:
            if isinstance(item, str):
                try:
                    ipaddress.IPv4Address(item.strip())
                    dns_servers.append(item.strip())
                except ValueError:
                    continue
    if not dns_servers:
        dns_servers = list(defaults.dns_servers)

    eth = raw.get("ethernet_interface", defaults.ethernet_interface)
    uplink = raw.get("uplink_interface", defaults.uplink_interface)

    return AppConfig(
        language=language,
        console_profile=profile,
        host_ip=host_ip,
        prefix_length=prefix_length,
        peer_ip=peer_ip,
        dns_servers=dns_servers,
        ethernet_interface=str(eth) if isinstance(eth, str) else "",
        uplink_interface=str(uplink) if isinstance(uplink, str) else "",
        http_bridge_enabled=bool(raw.get("http_bridge_enabled", False)),
        wizard_completed=bool(raw.get("wizard_completed", False)),
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
    """Atomically write UTF-8 config.json."""
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "language": cfg.language,
        "console_profile": cfg.console_profile,
        "host_ip": cfg.host_ip,
        "prefix_length": cfg.prefix_length,
        "peer_ip": cfg.peer_ip,
        "dns_servers": list(cfg.dns_servers),
        "ethernet_interface": cfg.ethernet_interface,
        "uplink_interface": cfg.uplink_interface,
        "http_bridge_enabled": cfg.http_bridge_enabled,
        "wizard_completed": cfg.wizard_completed,
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
