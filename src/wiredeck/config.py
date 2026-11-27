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
VALID_TRANSPORTS = ("wired", "wireless")
VALID_WIRELESS_SUBMODES = ("hotspot", "lan")
VALID_HOTSPOT_BANDS = ("5ghz", "2.4ghz", "auto")
VALID_RECORDER_BACKENDS = ("auto", "obs", "nvidia", "gamebar", "ffmpeg", "custom")
VALID_RECORDER_MODES = ("manual", "replay", "auto_record")
VALID_FFMPEG_FORMATS = ("mkv", "mp4", "mov", "ts", "flv")
VALID_FFMPEG_ENCODERS = ("auto", "av1_nvenc", "hevc_nvenc", "h264_nvenc", "libx264")
VALID_SPLIT_MODES = ("off", "time", "size", "both")
VALID_UPDATE_CHANNELS = ("stable", "beta")
DEFAULT_FILENAME_TEMPLATE = "{preset}_{date}_{time}"


@dataclass
class RecorderSettings:
    backend: str = "auto"
    mode: str = "replay"
    auto_link_quick_play: bool = False
    output_dir: str = ""
    fallback_dir: str = ""
    source_watch_dir: str = ""
    auto_offload: bool = True
    obs_exe_path: str = ""
    ffmpeg_exe_path: str = ""
    ffmpeg_format: str = "mkv"
    ffmpeg_encoder: str = "auto"
    ffmpeg_bitrate_mbps: int = 10
    ffmpeg_fps: int = 60
    custom_exe_path: str = ""
    hotkey_record: str = ""
    hotkey_replay: str = ""
    max_storage_gb: float = 0.0
    min_free_gb: float = 5.0
    auto_prune: bool = False
    split_mode: str = "off"
    split_minutes: int = 30
    split_size_mb: int = 2048
    filename_template: str = DEFAULT_FILENAME_TEMPLATE
    apply_naming_on_offload: bool = True
    ffmpeg_audio_device: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_recorder_settings(raw: Any) -> RecorderSettings:
    if not isinstance(raw, dict):
        return RecorderSettings()
    backend = str(raw.get("backend", "auto")).strip().lower()
    if backend not in VALID_RECORDER_BACKENDS:
        backend = "auto"
    mode = str(raw.get("mode", "replay")).strip().lower()
    if mode not in VALID_RECORDER_MODES:
        mode = "replay"
    fmt = str(raw.get("ffmpeg_format", "mkv")).strip().lower()
    if fmt not in VALID_FFMPEG_FORMATS:
        fmt = "mkv"
    enc = str(raw.get("ffmpeg_encoder", "auto")).strip().lower()
    if enc not in VALID_FFMPEG_ENCODERS:
        enc = "auto"
    split_mode = str(raw.get("split_mode", "off")).strip().lower()
    if split_mode not in VALID_SPLIT_MODES:
        split_mode = "off"
    try:
        bitrate = max(2, min(60, int(raw.get("ffmpeg_bitrate_mbps", 10))))
    except (TypeError, ValueError):
        bitrate = 10
    try:
        fps = 30 if int(raw.get("ffmpeg_fps", 60)) <= 30 else 60
    except (TypeError, ValueError):
        fps = 60
    try:
        split_minutes = max(1, min(240, int(raw.get("split_minutes", 30))))
    except (TypeError, ValueError):
        split_minutes = 30
    try:
        split_size_mb = max(50, min(32768, int(raw.get("split_size_mb", 2048))))
    except (TypeError, ValueError):
        split_size_mb = 2048
    try:
        max_gb = max(0.0, float(raw.get("max_storage_gb", 0.0)))
    except (TypeError, ValueError):
        max_gb = 0.0
    try:
        min_gb = max(0.5, min(200.0, float(raw.get("min_free_gb", 5.0))))
    except (TypeError, ValueError):
        min_gb = 5.0

    def _s(k: str) -> str:
        v = raw.get(k, "")
        return v.strip() if isinstance(v, str) else ""

    raw_tpl = _s("filename_template")[:96]
    if not raw_tpl:
        raw_tpl = DEFAULT_FILENAME_TEMPLATE

    return RecorderSettings(
        backend=backend,
        mode=mode,
        auto_link_quick_play=bool(raw.get("auto_link_quick_play", False)),
        output_dir=_s("output_dir"),
        fallback_dir=_s("fallback_dir"),
        source_watch_dir=_s("source_watch_dir"),
        auto_offload=bool(raw.get("auto_offload", True)),
        obs_exe_path=_s("obs_exe_path"),
        ffmpeg_exe_path=_s("ffmpeg_exe_path"),
        ffmpeg_format=fmt,
        ffmpeg_encoder=enc,
        ffmpeg_bitrate_mbps=bitrate,
        ffmpeg_fps=fps,
        custom_exe_path=_s("custom_exe_path"),
        hotkey_record=_s("hotkey_record"),
        hotkey_replay=_s("hotkey_replay"),
        max_storage_gb=max_gb,
        min_free_gb=min_gb,
        auto_prune=bool(raw.get("auto_prune", False)),
        split_mode=split_mode,
        split_minutes=split_minutes,
        split_size_mb=split_size_mb,
        filename_template=raw_tpl,
        apply_naming_on_offload=bool(raw.get("apply_naming_on_offload", True)),
        ffmpeg_audio_device=_s("ffmpeg_audio_device")[:120],
    )


@dataclass
class PresetSettings:
    host_ip: str = "192.168.50.2"
    prefix_length: int = 24
    peer_ip: str = "192.168.50.1"
    wireless_peer_ip: str = ""
    dns_servers: list[str] = field(default_factory=lambda: ["223.5.5.5", "8.8.8.8"])
    wizard_completed: bool = False
    preferred_client: str = "auto"
    custom_client_path: str = ""
    custom_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_preset_settings(preset_id: str) -> PresetSettings:
    p = get_preset(preset_id)
    return PresetSettings(
        host_ip=p.default_host_ip,
        prefix_length=p.default_prefix_length,
        peer_ip=p.default_peer_ip,
        wireless_peer_ip="",
        dns_servers=list(p.default_dns_servers),
        wizard_completed=False,
        preferred_client="auto",
        custom_client_path="",
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
        active_transport: str = "wired",
        wireless_submode: str = "hotspot",
        ethernet_interface: str = "",
        uplink_interface: str = "",
        wifi_interface: str = "",
        hotspot_prefer_5ghz: bool = True,
        hotspot_band: str | None = None,
        http_bridge_enabled: bool = False,
        onboarding_completed: bool = False,
        plugin_registry_url: str = "",
        update_channel: str = "stable",
        update_feed_url: str = "",
        auto_check_updates: bool = True,
        keep_awake_during_stream: bool = True,
        auto_restore_on_exit: bool = False,
        recorder: RecorderSettings | None = None,
        presets: dict[str, PresetSettings] | None = None,
        # Optional direct overrides for the active preset (clean ergonomics & legacy compat)
        host_ip: str | None = None,
        prefix_length: int | None = None,
        peer_ip: str | None = None,
        wireless_peer_ip: str | None = None,
        dns_servers: list[str] | None = None,
        wizard_completed: bool | None = None,
        custom_client_path: str | None = None,
    ) -> None:
        self.language = language if language in VALID_LANGUAGES else "zh-CN"
        self.theme = theme if theme in VALID_THEMES else "light"
        chosen = active_preset if active_preset is not None else (console_profile or "xbox")
        self.active_preset = normalize_preset_id(chosen)
        self.active_transport = active_transport if active_transport in VALID_TRANSPORTS else "wired"
        self.wireless_submode = wireless_submode if wireless_submode in VALID_WIRELESS_SUBMODES else "hotspot"
        self.ethernet_interface = ethernet_interface
        self.uplink_interface = uplink_interface
        self.wifi_interface = wifi_interface
        raw_band = str(hotspot_band).strip().lower() if isinstance(hotspot_band, str) else ""
        if raw_band in VALID_HOTSPOT_BANDS:
            self.hotspot_band = raw_band
            self.hotspot_prefer_5ghz = raw_band == "5ghz"
        else:
            self.hotspot_prefer_5ghz = bool(hotspot_prefer_5ghz)
            self.hotspot_band = "5ghz" if self.hotspot_prefer_5ghz else "auto"
        self.http_bridge_enabled = bool(http_bridge_enabled)
        self.onboarding_completed = bool(onboarding_completed)
        self.plugin_registry_url = plugin_registry_url.strip() if isinstance(plugin_registry_url, str) else ""
        ch = str(update_channel or "stable").strip().lower()
        self.update_channel = ch if ch in VALID_UPDATE_CHANNELS else "stable"
        self.update_feed_url = update_feed_url.strip() if isinstance(update_feed_url, str) else ""
        self.auto_check_updates = bool(auto_check_updates)
        self.keep_awake_during_stream = bool(keep_awake_during_stream)
        self.auto_restore_on_exit = bool(auto_restore_on_exit)
        self.recorder = recorder if isinstance(recorder, RecorderSettings) else RecorderSettings()

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
        if wireless_peer_ip is not None:
            active.wireless_peer_ip = _valid_optional_ipv4(wireless_peer_ip, active.wireless_peer_ip)
        if dns_servers is not None:
            active.dns_servers = _valid_dns_list(dns_servers, active.dns_servers)
        if wizard_completed is not None:
            active.wizard_completed = bool(wizard_completed)
        if custom_client_path is not None and isinstance(custom_client_path, str):
            active.custom_client_path = custom_client_path.strip()

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
    def wireless_peer_ip(self) -> str:
        return self.current_preset_settings.wireless_peer_ip

    @wireless_peer_ip.setter
    def wireless_peer_ip(self, value: str) -> None:
        self.current_preset_settings.wireless_peer_ip = _valid_optional_ipv4(
            value, self.current_preset_settings.wireless_peer_ip
        )

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
    def custom_client_path(self) -> str:
        return self.current_preset_settings.custom_client_path

    @custom_client_path.setter
    def custom_client_path(self, value: str) -> None:
        self.current_preset_settings.custom_client_path = value.strip() if isinstance(value, str) else ""

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
            "active_transport": self.active_transport,
            "wireless_submode": self.wireless_submode,
            "ethernet_interface": self.ethernet_interface,
            "uplink_interface": self.uplink_interface,
            "wifi_interface": self.wifi_interface,
            "hotspot_prefer_5ghz": self.hotspot_prefer_5ghz,
            "hotspot_band": self.hotspot_band,
            "http_bridge_enabled": self.http_bridge_enabled,
            "onboarding_completed": self.onboarding_completed,
            "plugin_registry_url": self.plugin_registry_url,
            "update_channel": self.update_channel,
            "update_feed_url": self.update_feed_url,
            "auto_check_updates": self.auto_check_updates,
            "keep_awake_during_stream": self.keep_awake_during_stream,
            "auto_restore_on_exit": self.auto_restore_on_exit,
            "recorder": self.recorder.to_dict(),
            # Flattened active-preset convenience fields for GUI/CLI/exporter consumers
            "host_ip": self.host_ip,
            "prefix_length": self.prefix_length,
            "peer_ip": self.peer_ip,
            "wireless_peer_ip": self.wireless_peer_ip,
            "dns_servers": list(self.dns_servers),
            "wizard_completed": self.wizard_completed,
            "custom_client_path": self.custom_client_path,
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


def _valid_optional_ipv4(value: Any, default: str) -> str:
    if not isinstance(value, str):
        return default
    stripped = value.strip()
    if stripped == "":
        return ""
    try:
        ipaddress.IPv4Address(stripped)
        return stripped
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
        wireless_peer_ip=_valid_optional_ipv4(raw.get("wireless_peer_ip"), base.wireless_peer_ip),
        dns_servers=_valid_dns_list(raw.get("dns_servers"), base.dns_servers),
        wizard_completed=bool(raw.get("wizard_completed", base.wizard_completed)),
        preferred_client=str(raw.get("preferred_client", "auto")) if isinstance(raw.get("preferred_client"), str) else "auto",
        custom_client_path=str(raw.get("custom_client_path", "")).strip() if isinstance(raw.get("custom_client_path"), str) else "",
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

    raw_transport = raw.get("active_transport", "wired")
    active_transport = raw_transport if raw_transport in VALID_TRANSPORTS else "wired"

    raw_submode = raw.get("wireless_submode", "hotspot")
    wireless_submode = raw_submode if raw_submode in VALID_WIRELESS_SUBMODES else "hotspot"

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
    if "wireless_peer_ip" in raw:
        active_ps.wireless_peer_ip = _valid_optional_ipv4(raw.get("wireless_peer_ip"), active_ps.wireless_peer_ip)
    if "dns_servers" in raw:
        active_ps.dns_servers = _valid_dns_list(raw.get("dns_servers"), active_ps.dns_servers)
    if "wizard_completed" in raw:
        active_ps.wizard_completed = bool(raw.get("wizard_completed"))
    if "custom_client_path" in raw and isinstance(raw.get("custom_client_path"), str):
        active_ps.custom_client_path = raw.get("custom_client_path", "").strip()

    eth = raw.get("ethernet_interface", "")
    uplink = raw.get("uplink_interface", "")
    wifi = raw.get("wifi_interface", "")

    return AppConfig(
        language=language,
        theme=theme,
        active_preset=active_preset,
        active_transport=active_transport,
        wireless_submode=wireless_submode,
        ethernet_interface=str(eth) if isinstance(eth, str) else "",
        uplink_interface=str(uplink) if isinstance(uplink, str) else "",
        wifi_interface=str(wifi) if isinstance(wifi, str) else "",
        hotspot_prefer_5ghz=bool(raw.get("hotspot_prefer_5ghz", True)),
        hotspot_band=raw.get("hotspot_band") if raw.get("hotspot_band") in VALID_HOTSPOT_BANDS else None,
        http_bridge_enabled=bool(raw.get("http_bridge_enabled", False)),
        onboarding_completed=bool(raw.get("onboarding_completed", False)),
        plugin_registry_url=str(raw.get("plugin_registry_url", "") or "").strip() if isinstance(raw.get("plugin_registry_url", ""), str) else "",
        update_channel=str(raw.get("update_channel", "stable") or "stable").strip().lower(),
        update_feed_url=str(raw.get("update_feed_url", "") or "").strip() if isinstance(raw.get("update_feed_url", ""), str) else "",
        auto_check_updates=bool(raw.get("auto_check_updates", True)),
        keep_awake_during_stream=bool(raw.get("keep_awake_during_stream", True)),
        auto_restore_on_exit=bool(raw.get("auto_restore_on_exit", False)),
        recorder=normalize_recorder_settings(raw.get("recorder")),
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
        "active_transport": cfg.active_transport,
        "wireless_submode": cfg.wireless_submode,
        "ethernet_interface": cfg.ethernet_interface,
        "uplink_interface": cfg.uplink_interface,
        "wifi_interface": cfg.wifi_interface,
        "hotspot_prefer_5ghz": cfg.hotspot_prefer_5ghz,
        "hotspot_band": cfg.hotspot_band,
        "http_bridge_enabled": cfg.http_bridge_enabled,
        "onboarding_completed": cfg.onboarding_completed,
        "plugin_registry_url": cfg.plugin_registry_url,
        "update_channel": cfg.update_channel,
        "update_feed_url": cfg.update_feed_url,
        "auto_check_updates": cfg.auto_check_updates,
        "keep_awake_during_stream": cfg.keep_awake_during_stream,
        "auto_restore_on_exit": cfg.auto_restore_on_exit,
        "recorder": cfg.recorder.to_dict(),
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
