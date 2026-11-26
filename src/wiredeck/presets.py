"""Declarative device preset registry (Xbox, PlayStation, Switch, Handheld, Other)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

ALL_PRESET_IDS = ("xbox", "playstation", "switch", "handheld", "other")

PRESET_ALIASES: dict[str, str] = {
    "xbox": "xbox",
    "xsx": "xbox",
    "xss": "xbox",
    "playstation": "playstation",
    "ps": "playstation",
    "ps5": "playstation",
    "ps4": "playstation",
    "switch": "switch",
    "ns": "switch",
    "nintendo": "switch",
    "handheld": "handheld",
    "deck": "handheld",
    "steamdeck": "handheld",
    "ally": "handheld",
    "other": "other",
    "generic": "other",
    "linux": "other",
}


@dataclass(frozen=True)
class PresetDefinition:
    id: str
    default_host_ip: str
    default_prefix_length: int
    default_peer_ip: str
    default_dns_servers: tuple[str, ...]
    uses_uwp_loopback: bool
    uses_stream_client: bool
    wizard_steps: tuple[str, ...]
    service_endpoint_url: str | None
    service_fault_code: str | None
    client_fault_code: str | None
    client_keys: tuple[str, ...]
    official_client_name: str
    official_client_url: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["default_dns_servers"] = list(self.default_dns_servers)
        d["wizard_steps"] = list(self.wizard_steps)
        d["client_keys"] = list(self.client_keys)
        return d


PRESETS: dict[str, PresetDefinition] = {
    "xbox": PresetDefinition(
        id="xbox",
        default_host_ip="192.168.50.2",
        default_prefix_length=24,
        default_peer_ip="192.168.50.1",
        default_dns_servers=("223.5.5.5", "8.8.8.8"),
        uses_uwp_loopback=True,
        uses_stream_client=True,
        wizard_steps=("nic", "uwp", "client", "peer"),
        service_endpoint_url="https://user.auth.xboxlive.com/user/authenticate",
        service_fault_code="XBOX_LIVE_UNREACHABLE",
        client_fault_code="STREAM_CLIENT_MISSING",
        client_keys=("xstreaming_path", "xbox_app_installed"),
        official_client_name="XStreamingDesktop",
        official_client_url="https://github.com/Geocld/XStreamingDesktop/releases/latest",
    ),
    "playstation": PresetDefinition(
        id="playstation",
        default_host_ip="192.168.50.2",
        default_prefix_length=24,
        default_peer_ip="192.168.50.10",
        default_dns_servers=("223.5.5.5", "8.8.8.8"),
        uses_uwp_loopback=False,
        uses_stream_client=True,
        wizard_steps=("nic", "client", "peer"),
        service_endpoint_url="https://auth.api.sonyentertainmentnetwork.com/",
        service_fault_code="PSN_UNREACHABLE",
        client_fault_code="PS_CLIENT_MISSING",
        client_keys=("chiaki_path", "ps_remote_path"),
        official_client_name="Chiaki-ng",
        official_client_url="https://github.com/streetpea/chiaki-ng/releases/latest",
    ),
    "switch": PresetDefinition(
        id="switch",
        default_host_ip="192.168.50.2",
        default_prefix_length=24,
        default_peer_ip="192.168.50.20",
        default_dns_servers=("223.5.5.5", "168.126.63.1"),
        uses_uwp_loopback=False,
        uses_stream_client=False,
        wizard_steps=("nic", "peer"),
        service_endpoint_url="http://ctest.cdn.nintendo.net/",
        service_fault_code="NINTENDO_UNREACHABLE",
        client_fault_code=None,
        client_keys=(),
        official_client_name="",
        official_client_url="",
    ),
    "handheld": PresetDefinition(
        id="handheld",
        default_host_ip="192.168.50.2",
        default_prefix_length=24,
        default_peer_ip="192.168.50.30",
        default_dns_servers=("223.5.5.5", "8.8.8.8"),
        uses_uwp_loopback=False,
        uses_stream_client=True,
        wizard_steps=("nic", "client", "peer"),
        service_endpoint_url=None,
        service_fault_code=None,
        client_fault_code="HANDHELD_CLIENT_MISSING",
        client_keys=("moonlight_path", "sunshine_path", "steam_path"),
        official_client_name="Moonlight",
        official_client_url="https://github.com/moonlight-stream/moonlight-qt/releases/latest",
    ),
    "other": PresetDefinition(
        id="other",
        default_host_ip="192.168.50.2",
        default_prefix_length=24,
        default_peer_ip="192.168.50.1",
        default_dns_servers=("223.5.5.5", "8.8.8.8"),
        uses_uwp_loopback=False,
        uses_stream_client=False,
        wizard_steps=("nic", "peer"),
        service_endpoint_url=None,
        service_fault_code=None,
        client_fault_code=None,
        client_keys=(),
        official_client_name="",
        official_client_url="",
    ),
}


def normalize_preset_id(raw: Any, default: str = "xbox") -> str:
    if not isinstance(raw, str):
        return default
    key = raw.strip().lower()
    return PRESET_ALIASES.get(key, default)


def get_preset(preset_id: str) -> PresetDefinition:
    return PRESETS[normalize_preset_id(preset_id)]


def list_presets() -> list[PresetDefinition]:
    return [PRESETS[pid] for pid in ALL_PRESET_IDS]
