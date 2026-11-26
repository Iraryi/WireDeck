"""GUI entrypoint (`WireDeck.exe`) using Edge WebView2 via pywebview."""

from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Callable

from wiredeck.actions import execute_action
from wiredeck.bridge import LocalBridgeServer
from wiredeck.config import AppConfig, load_config, save_config
from wiredeck.diagnostics import classify_snapshot, run_diagnosis
from wiredeck.exporter import export_diagnostic_bundle
from wiredeck.extensions import discover_extensions, inspect_extensions, run_extension
from wiredeck.models import ActionResult, DiagnosticReport, ExtensionRunResult, Severity
from wiredeck.paths import (
    AppPaths,
    get_assets_dir,
    get_cli_executable,
    get_web_dir,
    resolve_app_paths,
)
from wiredeck.presets import get_preset, list_presets, normalize_preset_id
from wiredeck.probes import _ping_peer, collect_system_snapshot
from wiredeck.ps_runner import is_admin, run_elevated_worker, run_powershell
from wiredeck.stream_client import (
    create_play_shortcut,
    create_restore_shortcut,
    download_and_verify_asset,
    fetch_latest_xstreaming_release,
    launch_stream_client,
)
from wiredeck.toolkits import discover_toolkits, run_toolkit
from wiredeck.version import APP_NAME, APP_VERSION, NAT_NAME
from wiredeck.wireless_diagnostics import classify_wireless_snapshot, run_wireless_diagnosis
from wiredeck.wireless_probes import collect_wireless_snapshot, get_hotspot_credentials, measure_wifi_jitter


def build_preview_report(preview_mode: str, preset_id: str = "xbox") -> DiagnosticReport:
    """Build deterministic, zero-PII diagnostic reports for clean high-DPI screenshots."""
    cfg = AppConfig(active_preset=preset_id)
    base_snap: dict[str, Any] = {
        "os_version": "Windows 11 24H2",
        "is_admin": False,
        "net_adapters": [
            {
                "name": "Ethernet",
                "interface_description": "2.5GbE PCIe Controller",
                "interface_index": 12,
                "status": "Up",
                "link_speed_bps": 1_000_000_000,
                "link_speed_text": "1 Gbps",
                "pnp_device_id": "PCI\\VEN_10EC&DEV_8125",
                "is_virtual": False,
                "is_wireless": False,
                "rx_errors": 0,
                "tx_errors": 0,
                "rx_discards": 0,
                "tx_discards": 0,
                "powersave_enabled": [],
            },
            {
                "name": "Wi-Fi",
                "interface_description": "Wi-Fi 6E Adapter",
                "interface_index": 8,
                "status": "Up",
                "link_speed_bps": 866_000_000,
                "link_speed_text": "866 Mbps",
                "pnp_device_id": "PCI\\VEN_8086&DEV_51F1",
                "is_virtual": False,
                "is_wireless": True,
                "rx_errors": 0,
                "tx_errors": 0,
                "rx_discards": 0,
                "tx_discards": 0,
                "powersave_enabled": [],
            },
        ],
        "pnp_ethernet_devices": [],
        "ipv4_addresses": [
            {"interface_alias": "Ethernet", "interface_index": 12, "ip": cfg.host_ip, "prefix_length": 24},
            {"interface_alias": "Wi-Fi", "interface_index": 8, "ip": "192.168.1.100", "prefix_length": 24},
        ],
        "default_routes": [
            {
                "interface_alias": "Wi-Fi",
                "interface_index": 8,
                "next_hop": "192.168.1.1",
                "metric": 35,
                "is_virtual_or_tun": False,
                "interface_description": "Wi-Fi 6E Adapter",
            }
        ],
        "ip_forwarding": {"Ethernet": True, "Wi-Fi": True},
        "ics_sharing_interfaces": [],
        "shared_access_service_status": "Stopped",
        "udp_listeners": [],
        "net_nats": [{"name": NAT_NAME, "internal_subnet": "192.168.50.0/24"}],
        "firewall_rules": {"WireDeck-WinNAT-Allow": True, "WireDeck-Direct-Allow": True},
        "uwp_exempt_packages": [],
        "uwp_missing_packages": [],
        "stream_clients": {
            "xstreaming_path": "C:\\Program Files\\XStreaming\\XStreaming.exe",
            "xbox_app_installed": True,
            "chiaki_path": "C:\\Program Files\\chiaki-ng\\chiaki.exe",
            "ps_remote_path": None,
            "moonlight_path": "C:\\Program Files\\Moonlight Game Streaming\\Moonlight.exe",
            "sunshine_path": None,
            "steam_path": None,
        },
        "connectivity": {
            "peer_reachable": True,
            "peer_rtt_ms": 0.4,
            "host_internet_ok": True,
            "xbox_live_ok": True,
            "service_endpoint_ok": True,
            "service_endpoint_status": 200,
        },
    }

    if preview_mode == "breakpoint":
        base_snap["net_nats"] = []
        base_snap["ip_forwarding"] = {"Ethernet": False, "Wi-Fi": False}
        base_snap["default_routes"].append(
            {
                "interface_alias": "VPN-TUN",
                "interface_index": 33,
                "next_hop": "172.19.0.2",
                "metric": 5,
                "is_virtual_or_tun": True,
                "interface_description": "sing-tun Virtual Adapter",
            }
        )
        return classify_snapshot(base_snap, cfg)

    if preview_mode == "wizard":
        base_snap["uwp_missing_packages"] = ["microsoft.windowsstore_8wekyb3d8bbwe"]
        base_snap["net_nats"] = []
        base_snap["connectivity"]["peer_reachable"] = False
        return classify_snapshot(base_snap, cfg)

    if preview_mode == "ai":
        ext_demo = [
            ExtensionRunResult(
                ext_id="fix-intel-i225",
                name="修复 Intel I225-V 节能断流",
                author="DSH",
                layer="L1",
                status=Severity.OK,
                code="OK",
                title="I225 正常",
                message="节能项已关闭",
                fix_available=True,
                requires_admin=True,
            )
        ]
        return classify_snapshot(base_snap, cfg, ext_results=ext_demo)

    return classify_snapshot(base_snap, cfg)


def build_wireless_preview_report(
    preview_mode: str,
    preset_id: str = "xbox",
    submode: str = "hotspot",
) -> DiagnosticReport:
    """Build deterministic, zero-PII wireless reports for high-DPI screenshots and tests."""
    is_lan = (submode == "lan") or (preview_mode == "wireless_lan")
    cfg = AppConfig(
        active_preset=preset_id,
        active_transport="wireless",
        wireless_submode="lan" if is_lan else "hotspot",
        wireless_peer_ip="192.168.1.88" if is_lan else "",
    )
    snap: dict[str, Any] = {
        "is_admin": False,
        "wifi_adapters": [
            {
                "name": "WLAN",
                "interface_description": "Intel(R) Wi-Fi 6 AX201 160MHz",
                "interface_index": 11,
                "status": "Up",
                "link_speed_bps": 866_000_000,
                "link_speed_text": "866 Mbps",
                "pnp_device_id": "PCI\\VEN_8086&DEV_A0F0",
                "advanced_properties": [],
                "powersave_enabled": ["MIMOPowerSaveMode", "*PacketCoalescing"] if is_lan else [],
            }
        ],
        "wlan": {
            "name": "WLAN",
            "description": "Intel(R) Wi-Fi 6 AX201 160MHz",
            "state": "connected",
            "connected": True,
            "ssid": "Studio-5G",
            "band": "5 GHz",
            "band_ghz": 5.0,
            "channel": 157,
            "radio_type": "802.11ax",
            "rx_rate_mbps": 866.0,
            "tx_rate_mbps": 866.0,
            "signal_pct": 84,
            "rssi_dbm": -52,
        },
        "autoconfig_enabled": True if is_lan else False,
        "ipv4_addresses": [{"interface_alias": "WLAN", "interface_index": 11, "ip": "192.168.1.105", "prefix_length": 24}],
        "wifi_gateway_ip": "192.168.1.1",
        "default_routes": [
            {
                "interface_alias": "WLAN",
                "interface_index": 11,
                "next_hop": "192.168.1.1",
                "metric": 35,
                "is_virtual_or_tun": False,
                "interface_description": "Intel(R) Wi-Fi 6 AX201 160MHz",
            }
        ],
        "wired_nat_active": False,
        "hotspot": {
            "supported": True,
            "band_5ghz_supported": True,
            "capability": "Enabled",
            "state": "Off" if is_lan else "On",
            "band": "FiveGigahertz" if not is_lan else "Auto",
            "ssid": "WireDeck-5G",
            "client_count": 0 if is_lan else 1,
            "max_client_count": 8,
            "virtual_adapter_name": "Microsoft Wi-Fi Direct Virtual Adapter",
            "virtual_adapter_ip": "" if is_lan else "192.168.137.1",
        },
        "hotspot_clients": []
        if is_lan
        else [{"ip": "192.168.137.42", "mac": "00-11-22-33-44-55", "state": "Reachable", "interface_alias": "WLAN-Direct"}],
        "uwp_exempt_packages": [],
        "uwp_missing_packages": ["microsoft.windowsstore_8wekyb3d8bbwe"] if preview_mode == "wireless_wizard" else [],
        "stream_clients": {
            "xstreaming_path": "C:\\Program Files\\XStreaming\\XStreaming.exe",
            "xbox_app_installed": True,
            "chiaki_path": "C:\\Program Files\\chiaki-ng\\chiaki.exe",
            "ps_remote_path": None,
            "moonlight_path": "C:\\Program Files\\Moonlight Game Streaming\\Moonlight.exe",
            "sunshine_path": None,
            "steam_path": None,
        },
        "connectivity": {
            "target_peer_ip": "192.168.1.88" if is_lan else "192.168.137.42",
            "peer_reachable": False if preview_mode == "wireless_wizard" else True,
            "peer_rtt_ms": None if preview_mode == "wireless_wizard" else (2.4 if is_lan else 1.8),
            "jitter_ms": None if preview_mode == "wireless_wizard" else (1.4 if is_lan else 0.9),
            "loss_pct": 0.0,
            "gateway_ip": "192.168.1.1",
            "gateway_reachable": True,
            "host_internet_ok": True,
            "xbox_live_ok": True,
            "service_endpoint_ok": True,
            "service_endpoint_status": 200,
        },
    }
    return classify_wireless_snapshot(snap, cfg)


class WireDeckApi:
    """Python API surface exposed to the WebView2 frontend via pywebview."""

    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        snapshot_fn: Callable[..., dict[str, Any]] = collect_system_snapshot,
        wireless_snapshot_fn: Callable[..., dict[str, Any]] = collect_wireless_snapshot,
        preview_state: str | None = None,
        preview_wizard_step: int | None = None,
    ) -> None:
        self.paths = paths or resolve_app_paths()
        self.cfg = load_config(self.paths)
        self._snapshot_fn = snapshot_fn
        self._wireless_snapshot_fn = wireless_snapshot_fn
        self._preview_state = preview_state
        self._preview_wizard_step = preview_wizard_step
        self._last_report: DiagnosticReport | None = None
        self._last_wireless_report: DiagnosticReport | None = None
        self._bridge: LocalBridgeServer | None = None
        self._window: Any = None
        if preview_state in ("wireless", "wireless_wizard"):
            self.cfg.active_transport = "wireless"
            self.cfg.wireless_submode = "hotspot"
        elif preview_state == "wireless_lan":
            self.cfg.active_transport = "wireless"
            self.cfg.wireless_submode = "lan"
        elif preview_state in ("ready", "breakpoint", "wizard"):
            self.cfg.active_transport = "wired"

    def _preview_view_name(self) -> str | None:
        if self._preview_state in ("wizard", "ai", "settings"):
            return self._preview_state
        if self._preview_state in ("wireless", "wireless_lan"):
            return "wireless"
        if self._preview_state == "wireless_wizard":
            return "wizard"
        return None

    def get_state(self) -> dict[str, Any]:
        bridge_running = self._bridge is not None and self._bridge.is_running
        bridge_info: dict[str, Any] = {
            "running": bridge_running,
            "url": self._bridge.info.url if (bridge_running and self._bridge.info) else "",
            "token": self._bridge.info.token if (bridge_running and self._bridge.info) else "",
            "pending_jobs": self._bridge.list_pending_jobs() if bridge_running else [],
        }
        active_preset = get_preset(self.cfg.active_preset)
        show_onboarding = (
            True
            if self._preview_state == "onboarding"
            else (False if self._preview_state else (not self.cfg.onboarding_completed))
        )
        return {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "is_admin": is_admin(),
            "config": self.cfg.to_dict(),
            "active_preset": active_preset.to_dict(),
            "presets_meta": [p.to_dict() for p in list_presets()],
            "show_onboarding": show_onboarding,
            "paths": self.paths.to_dict(),
            "bridge": bridge_info,
            "preview_view": self._preview_view_name(),
            "preview_wizard_step": self._preview_wizard_step,
        }

    def diagnose(self) -> dict[str, Any]:
        if self._preview_state:
            rep = build_preview_report(self._preview_state, preset_id=self.cfg.active_preset)
        else:
            rep = run_diagnosis(
                self.paths,
                self.cfg,
                include_extensions=True,
                probe_network=True,
                snapshot_fn=self._snapshot_fn,
            )
        self._last_report = rep
        return rep.to_dict()

    def diagnose_wireless(self) -> dict[str, Any]:
        if self._preview_state:
            rep = build_wireless_preview_report(
                self._preview_state,
                preset_id=self.cfg.active_preset,
                submode=self.cfg.wireless_submode,
            )
        else:
            rep = run_wireless_diagnosis(
                self.paths,
                self.cfg,
                include_extensions=True,
                probe_network=True,
                snapshot_fn=self._wireless_snapshot_fn,
            )
        self._last_wireless_report = rep
        return rep.to_dict()

    def get_hotspot_credentials(self) -> dict[str, Any]:
        if self._preview_state:
            return {
                "ok": True,
                "ssid": "WireDeck-5G",
                "passphrase": "88482026",
                "band": "FiveGigahertz",
                "state": "Off" if self._preview_state == "wireless_lan" else "On",
                "client_count": 0 if self._preview_state == "wireless_lan" else 1,
            }
        return get_hotspot_credentials()

    def set_transport(self, transport: str) -> dict[str, Any]:
        if transport in ("wired", "wireless"):
            self.cfg.active_transport = transport
            save_config(self.paths, self.cfg)
        return {"ok": True, "active_transport": self.cfg.active_transport, "config": self.cfg.to_dict()}

    def set_wireless_submode(self, submode: str) -> dict[str, Any]:
        if submode in ("hotspot", "lan"):
            self.cfg.wireless_submode = submode
            save_config(self.paths, self.cfg)
        return {"ok": True, "wireless_submode": self.cfg.wireless_submode, "config": self.cfg.to_dict()}

    def ping_wireless_peer_now(self, peer_ip: str = "") -> dict[str, Any]:
        target = peer_ip.strip() or self.cfg.wireless_peer_ip
        if not target and self._last_wireless_report:
            target = str((self._last_wireless_report.snapshot.get("connectivity") or {}).get("target_peer_ip") or "")
        if not target:
            return {"ok": False, "code": "NO_PEER_IP", "target_ip": ""}
        if self._preview_state:
            return {"ok": True, "target_ip": target, "reachable": True, "avg_rtt_ms": 2.1, "jitter_ms": 0.9, "loss_pct": 0.0}
        res = measure_wifi_jitter(target, count=6, timeout_ms=700)
        return {"ok": bool(res.get("reachable")), **res}

    def _execute_or_elevate(self, action: str, opts: dict[str, Any]) -> ActionResult:
        force = bool(opts.get("force", False))
        ext_id = opts.get("ext_id")
        if is_admin():
            return execute_action(
                action,
                self.paths,
                self.cfg,
                force=force,
                ext_id=str(ext_id) if ext_id else None,
                runner=run_powershell,
                snapshot_fn=self._snapshot_fn,
            )

        job_id = uuid.uuid4().hex[:10]
        req_path = self.paths.runtime_dir / f"req_{job_id}.json"
        out_path = self.paths.runtime_dir / f"res_{job_id}.json"
        self.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        req_path.write_text(
            json.dumps(
                {
                    "action": action,
                    "force": force,
                    "ext_id": ext_id,
                    "data_dir": str(self.paths.data_dir),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        try:
            return run_elevated_worker(get_cli_executable(self.paths.exe_dir), req_path, out_path)
        finally:
            for f in (req_path, out_path):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass

    def run_action(self, action: str, opts: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下不修改本机网络").to_dict()
        return self._execute_or_elevate(action, opts or {}).to_dict()

    def complete_onboarding(self, language: str, preset_id: str, transport: str = "wired") -> dict[str, Any]:
        """Save initial startup language, chosen transport & preset, and mark onboarding complete."""
        from wiredeck.config import VALID_LANGUAGES, VALID_TRANSPORTS

        if language in VALID_LANGUAGES:
            self.cfg.language = language
        if transport in VALID_TRANSPORTS:
            self.cfg.active_transport = transport
        self.cfg.switch_preset(preset_id)
        self.cfg.onboarding_completed = True
        save_config(self.paths, self.cfg)
        return {"ok": True, "config": self.cfg.to_dict(), "active_preset": get_preset(self.cfg.active_preset).to_dict()}

    def switch_preset(self, preset_id: str) -> dict[str, Any]:
        """Switch active device preset cleanly without overwriting other presets' settings."""
        self.cfg.switch_preset(preset_id)
        save_config(self.paths, self.cfg)
        return {"ok": True, "config": self.cfg.to_dict(), "active_preset": get_preset(self.cfg.active_preset).to_dict()}

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        from wiredeck.config import VALID_LANGUAGES, VALID_THEMES, VALID_TRANSPORTS, VALID_WIRELESS_SUBMODES

        # Handle preset switch cleanly FIRST so we don't copy old preset IPs onto the newly chosen preset
        requested_preset = patch.get("active_preset", patch.get("console_profile"))
        switching_only = requested_preset is not None and normalize_preset_id(requested_preset) != self.cfg.active_preset
        if switching_only:
            self.cfg.switch_preset(str(requested_preset))

        if patch.get("language") in VALID_LANGUAGES:
            self.cfg.language = str(patch["language"])
        lang = self.cfg.language

        if "wireless_peer_ip" in patch:
            w_in = str(patch["wireless_peer_ip"] or "").strip()
            if w_in:
                try:
                    ipaddress.IPv4Address(w_in)
                except ValueError:
                    err_w = {
                        "zh-CN": "无线设备 IPv4 格式无效",
                        "zh-TW": "無線裝置 IPv4 格式無效",
                        "ja": "無線デバイス IPv4 の形式が無効です",
                        "en": "Invalid wireless device IPv4 format",
                    }
                    return {"ok": False, "code": "INVALID_IP", "message": err_w.get(lang, err_w["en"])}
            self.cfg.wireless_peer_ip = w_in

        # Validate IP inputs strictly when provided (or for current active preset)
        host_in = str(patch.get("host_ip", self.cfg.host_ip)).strip()
        peer_in = str(patch.get("peer_ip", self.cfg.peer_ip)).strip()
        try:
            host_addr = ipaddress.IPv4Address(host_in)
            peer_addr = ipaddress.IPv4Address(peer_in)
        except ValueError:
            err_invalid = {
                "zh-CN": "IPv4 地址格式无效（示例：192.168.50.2）",
                "zh-TW": "IPv4 位址格式無效（範例：192.168.50.2）",
                "ja": "IPv4 アドレスの形式が無効です（例：192.168.50.2）",
                "en": "Invalid IPv4 address format (e.g. 192.168.50.2)",
            }
            return {
                "ok": False,
                "code": "INVALID_IP",
                "message": err_invalid.get(lang, err_invalid["en"]),
            }
        if host_addr == peer_addr:
            err_same = {
                "zh-CN": "电脑有线 IP 与设备 IP 不能相同",
                "zh-TW": "電腦有線 IP 與裝置 IP 不能相同",
                "ja": "PC の有線 IP とデバイスの IP は同じにできません",
                "en": "Host IP and Device IP cannot be identical",
            }
            return {
                "ok": False,
                "code": "SAME_IP",
                "message": err_same.get(lang, err_same["en"]),
            }
        net_host = ipaddress.IPv4Interface(f"{host_addr}/{self.cfg.prefix_length}").network
        if peer_addr not in net_host:
            err_sub = {
                "zh-CN": f"设备 IP 必须与电脑在同网段（{net_host}）",
                "zh-TW": f"裝置 IP 必須與電腦在同網段（{net_host}）",
                "ja": f"デバイス IP は PC と同じサブネットである必要があります（{net_host}）",
                "en": f"Device IP must be in the same subnet ({net_host})",
            }
            return {
                "ok": False,
                "code": "SUBNET_MISMATCH",
                "message": err_sub.get(lang, err_sub["en"]),
            }

        if patch.get("theme") in VALID_THEMES:
            self.cfg.theme = str(patch["theme"])
        if patch.get("active_transport") in VALID_TRANSPORTS:
            self.cfg.active_transport = str(patch["active_transport"])
        if patch.get("wireless_submode") in VALID_WIRELESS_SUBMODES:
            self.cfg.wireless_submode = str(patch["wireless_submode"])
        if "ethernet_interface" in patch:
            self.cfg.ethernet_interface = str(patch["ethernet_interface"] or "")
        if "uplink_interface" in patch:
            self.cfg.uplink_interface = str(patch["uplink_interface"] or "")
        if "wifi_interface" in patch:
            self.cfg.wifi_interface = str(patch["wifi_interface"] or "")
        if "hotspot_prefer_5ghz" in patch:
            self.cfg.hotspot_prefer_5ghz = bool(patch["hotspot_prefer_5ghz"])
        if "onboarding_completed" in patch:
            self.cfg.onboarding_completed = bool(patch["onboarding_completed"])
        if "custom_client_path" in patch:
            self.cfg.custom_client_path = str(patch["custom_client_path"] or "").strip()

        if "host_ip" in patch:
            self.cfg.host_ip = str(host_addr)
        if "peer_ip" in patch:
            self.cfg.peer_ip = str(peer_addr)
        if "dns_servers" in patch:
            raw_dns = patch["dns_servers"]
            if isinstance(raw_dns, str):
                parts = [p.strip() for p in raw_dns.replace("/", ",").replace(" ", ",").split(",") if p.strip()]
                self.cfg.dns_servers = parts
            elif isinstance(raw_dns, list):
                self.cfg.dns_servers = raw_dns

        save_config(self.paths, self.cfg)
        _set_titlebar_theme(f"{APP_NAME} v{APP_VERSION}", self.cfg.theme == "dark")
        return {"ok": True, "config": self.cfg.to_dict(), "active_preset": get_preset(self.cfg.active_preset).to_dict()}

    def list_toolkits(self) -> list[dict[str, Any]]:
        """List bundled read-only AI toolkits for the active preset + universal common tools."""
        return [t.to_dict() for t in discover_toolkits(preset_id=self.cfg.active_preset, include_common=True)]

    def run_toolkit(self, tool_id: str) -> dict[str, Any]:
        """Execute a bundled read-only AI toolkit script and return JSON result."""
        return run_toolkit(tool_id, cfg=self.cfg).to_dict()

    def ping_peer_now(self) -> dict[str, Any]:
        """Fast <1s direct peer ping for Step 4 connectivity verification."""
        if self._preview_state:
            return {"ok": True, "peer_ip": self.cfg.peer_ip, "rtt_ms": 0.4}
        reachable, rtt_ms = _ping_peer(self.cfg.peer_ip, timeout_ms=800)
        return {"ok": reachable, "peer_ip": self.cfg.peer_ip, "rtt_ms": rtt_ms}

    def export_for_ai(self) -> dict[str, Any]:
        if self.cfg.active_transport == "wireless":
            report = self._last_wireless_report or (
                build_wireless_preview_report(self._preview_state or "wireless", preset_id=self.cfg.active_preset, submode=self.cfg.wireless_submode)
                if self._preview_state
                else run_wireless_diagnosis(
                    self.paths,
                    self.cfg,
                    include_extensions=True,
                    probe_network=True,
                    snapshot_fn=self._wireless_snapshot_fn,
                )
            )
        else:
            report = self._last_report or (
                build_preview_report(self._preview_state, preset_id=self.cfg.active_preset)
                if self._preview_state
                else run_diagnosis(self.paths, self.cfg, include_extensions=True, probe_network=True, snapshot_fn=self._snapshot_fn)
            )
        bundle_path, prompt = export_diagnostic_bundle(self.paths, report, self.cfg)
        return {"ok": True, "bundle_path": str(bundle_path), "prompt": prompt}

    def list_extensions(self) -> list[dict[str, Any]]:
        if self._preview_state == "ai":
            return [
                {
                    "valid": True,
                    "errors": [],
                    "manifest": {
                        "id": "fix-intel-i225",
                        "name": "修复 Intel I225-V 节能断流",
                        "author": "DSH",
                        "layer": "L1",
                        "check_script": "check.ps1",
                        "fix_script": "fix.ps1",
                        "description": "检测并关闭 Intel I225-V 特定节能注册表项",
                    },
                },
                {
                    "valid": True,
                    "errors": [],
                    "manifest": {
                        "id": "campus-mtu-check",
                        "name": "校园网 MTU 探测与优化",
                        "author": "DSH",
                        "layer": "L5",
                        "check_script": "check.ps1",
                        "fix_script": None,
                        "description": "探测上游出口路径 MTU，定位分片丢包",
                    },
                },
            ]
        return inspect_extensions(self.paths)

    def run_extension(self, ext_id: str, mode: str = "check") -> dict[str, Any]:
        if mode == "fix":
            return self.run_action("run-ext", {"ext_id": ext_id})
        manifests = {m.id: m for m in discover_extensions(self.paths)}
        m = manifests.get(ext_id)
        if m is None:
            return {"status": "fail", "code": "EXT_NOT_FOUND", "message": f"未找到扩展 {ext_id}"}
        return run_extension(m, mode="check").to_dict()

    def create_sample_extension(self) -> dict[str, Any]:
        """Create a harmless read-only example extension so first-time users can see hot-reload work."""
        ext_dir = self.paths.extensions_dir / "sample-dns-check"
        manifest_path = ext_dir / "ext.json"
        script_path = ext_dir / "check.ps1"
        if manifest_path.is_file() and script_path.is_file():
            return {
                "ok": True,
                "created": False,
                "ext_id": "sample-dns-check",
                "message": "示例扩展已存在，可直接点「检测」体验",
            }
        ext_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema": "wiredeck-ext/v1",
            "id": "sample-dns-check",
            "name": "示例：公共 DNS 连通自检",
            "author": "WireDeck",
            "enabled": True,
            "auto_check": False,
            "layer": "L5",
            "overrides_faults": [],
            "requires_admin": False,
            "timeout_sec": 5,
            "check_script": "check.ps1",
            "description": "只读示例扩展：检测 223.5.5.5 连通性，演示热加载插件写法（可随时在扩展目录删除）",
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        ps_code = (
            "$ok = Test-Connection -ComputerName 223.5.5.5 -Count 1 -Quiet -ErrorAction SilentlyContinue\r\n"
            "if ($ok) {\r\n"
            "  Write-Output '{\"status\":\"ok\",\"code\":\"SAMPLE_DNS_OK\",\"title\":\"公共 DNS 正常\",\"message\":\"223.5.5.5 连通正常（示例扩展运行成功）\"}'\r\n"
            "} else {\r\n"
            "  Write-Output '{\"status\":\"warn\",\"code\":\"SAMPLE_DNS_TIMEOUT\",\"title\":\"公共 DNS 超时\",\"message\":\"无法 ping 通 223.5.5.5，可检查上游网络\"}'\r\n"
            "}\r\n"
        )
        script_path.write_bytes(b"\xef\xbb\xbf" + ps_code.encode("utf-8"))
        return {
            "ok": True,
            "created": True,
            "ext_id": "sample-dns-check",
            "message": "已创建只读示例扩展，点击下方「检测」即可体验",
        }

    def toggle_bridge(self, enabled: bool) -> dict[str, Any]:
        if enabled:
            if self._bridge is None:
                self._bridge = LocalBridgeServer(
                    paths=self.paths,
                    diagnose_fn=lambda: run_diagnosis(
                        self.paths, self.cfg, include_extensions=True, probe_network=True, snapshot_fn=self._snapshot_fn
                    ),
                    diagnose_wireless_fn=lambda: run_wireless_diagnosis(
                        self.paths, self.cfg, include_extensions=True, probe_network=True, snapshot_fn=self._wireless_snapshot_fn
                    ),
                    execute_action_fn=self._execute_or_elevate,
                )
            info = self._bridge.start()
            return {"ok": True, "running": True, "url": info.url, "token": info.token, "pending_jobs": []}
        else:
            if self._bridge is not None:
                self._bridge.stop()
            return {"ok": True, "running": False, "url": "", "token": "", "pending_jobs": []}

    def approve_bridge_job(self, job_id: str, allow: bool) -> dict[str, Any]:
        if self._bridge is None:
            return {"ok": False, "code": "BRIDGE_OFF"}
        job = self._bridge.approve_job(job_id) if allow else self._bridge.reject_job(job_id)
        return {"ok": True, "job": job.to_dict() if job else None}

    def download_xstreaming(self) -> dict[str, Any]:
        preset = get_preset(self.cfg.active_preset)
        if preset.id != "xbox":
            if preset.official_client_url:
                webbrowser.open(preset.official_client_url)
                return {
                    "ok": True,
                    "code": "OPENED_RELEASE_PAGE",
                    "message": f"已打开 {preset.official_client_name} 官方发布页",
                }
            return {"ok": True, "code": "NO_CLIENT_NEEDED", "message": "当前预设无需下载额外串流端"}
        info = fetch_latest_xstreaming_release()
        if not info.ok or not info.download_url:
            webbrowser.open(info.release_page_url)
            return {"ok": False, "code": "RELEASE_FETCH_FAILED", "message": info.error}
        downloads = Path.home() / "Downloads"
        dest = downloads / (info.asset_name or "XStreaming-Setup.exe")
        try:
            saved = download_and_verify_asset(info.download_url, dest, info.sha256)
            os.startfile(str(saved))  # type: ignore[attr-defined]
            return {"ok": True, "code": "DOWNLOADED", "message": f"官方安装包已通过 SHA-256 校验并打开：{saved.name}"}
        except Exception as exc:
            webbrowser.open(info.release_page_url)
            return {"ok": False, "code": "DOWNLOAD_FAILED", "message": f"下载校验失败（{exc}），已打开官方发布页"}

    def launch_stream_client(self) -> dict[str, Any]:
        return launch_stream_client(
            preset_id=self.cfg.active_preset,
            custom_path=self.cfg.custom_client_path,
        ).to_dict()

    def pick_stream_client(self) -> dict[str, Any]:
        """Prompt user to pick a local custom streaming client .exe and save it per-preset."""
        if self._preview_state:
            return {"ok": True, "code": "PREVIEW_ONLY", "path": "D:\\Games\\XStreaming\\XStreaming.exe", "filename": "XStreaming.exe"}
        if self._window is None:
            return {"ok": False, "code": "NO_WINDOW"}
        try:
            import webview

            picked = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("Executable (*.exe)", "All files (*.*)"),
            )
            if not picked:
                return {"ok": False, "code": "CANCELLED"}
            chosen = str(picked[0] if isinstance(picked, (list, tuple)) else picked).strip()
            if not chosen or not Path(chosen).is_file():
                return {"ok": False, "code": "INVALID_FILE"}
            self.cfg.custom_client_path = chosen
            save_config(self.paths, self.cfg)
            return {"ok": True, "code": "CUSTOM_CLIENT_SAVED", "path": chosen, "filename": Path(chosen).name}
        except Exception as exc:
            return {"ok": False, "code": "PICK_FAILED", "message": str(exc)}

    def quick_play_now(self) -> dict[str, Any]:
        """Activate configured connection mode if idle & launch streaming client cleanly."""
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下模拟已开网并启动客户端").to_dict()
        done, fallback_view = _run_quick_play(
            self,
            preset_id=self.cfg.active_preset,
            transport=self.cfg.active_transport,
            submode=self.cfg.wireless_submode,
        )
        return {
            "ok": done,
            "code": "QUICK_PLAY_OK" if done else "QUICK_PLAY_BLOCKED",
            "fallback_view": fallback_view,
            "message": "" if done else "存在前置阻断或未找到客户端，已定位到需要处理的步骤",
        }

    def create_play_shortcut(self) -> dict[str, Any]:
        """Create a Desktop .lnk Quick-Play shortcut for the current preset and transport."""
        if self._preview_state:
            from wiredeck.stream_client import build_play_shortcut_name

            fname = build_play_shortcut_name(self.cfg)
            return ActionResult(
                ok=True,
                code="PREVIEW_ONLY",
                message=f"预览模式下模拟已创建：{fname}",
                details={"filename": fname},
            ).to_dict()
        return create_play_shortcut(self.paths, self.cfg).to_dict()

    def create_restore_shortcut(self) -> dict[str, Any]:
        """Create a Desktop .lnk shortcut that restores normal Ethernet & Wi-Fi state."""
        if self._preview_state:
            from wiredeck.stream_client import build_restore_shortcut_name

            fname = build_restore_shortcut_name(self.cfg)
            return ActionResult(
                ok=True,
                code="PREVIEW_ONLY",
                message=f"预览模式下模拟已创建：{fname}",
                details={"filename": fname},
            ).to_dict()
        return create_restore_shortcut(self.paths, self.cfg).to_dict()

    def open_folder(self, kind: str) -> dict[str, Any]:
        mapping = {
            "extensions": self.paths.extensions_dir,
            "logs": self.paths.logs_dir,
            "exports": self.paths.exports_dir,
            "data": self.paths.data_dir,
        }
        target = mapping.get(kind, self.paths.data_dir)
        target.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def copy_to_clipboard(self, text: str) -> dict[str, Any]:
        try:
            subprocess.run(
                ["clip"],
                input=b"\xff\xfe" + text.encode("utf-16le"),
                check=False,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            return {"ok": True}
        except Exception:
            return {"ok": False}

    def shutdown(self) -> None:
        if self._bridge is not None:
            self._bridge.stop()


def _set_titlebar_theme(title: str, dark: bool) -> None:
    """Sync Windows 11/10 native title bar theme (light or neutral dark)."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        for _ in range(15):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                val = ctypes.c_int(1 if dark else 0)
                dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
                return
            time.sleep(0.08)
    except Exception:
        pass


def _capture_window_screenshot(title_prefix: str, out_path: Path, delay_sec: float = 2.2) -> None:
    """Capture crisp real window image via Windows PrintWindow (PW_RENDERFULLCONTENT) for Task 8."""
    time.sleep(delay_sec)
    try:
        from PIL import Image

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hwnd = user32.FindWindowW(None, title_prefix)
        if not hwnd:
            return

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = max(1, rect.right - rect.left)
        h = max(1, rect.bottom - rect.top)

        hdc_win = user32.GetWindowDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
        gdi32.SelectObject(hdc_mem, hbmp)

        # PW_RENDERFULLCONTENT = 2 captures Edge WebView2 GPU-composited pixels cleanly!
        user32.PrintWindow(hwnd, hdc_mem, 2)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h  # top-down DIB
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)

        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, format="PNG")

        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)
    except Exception:
        pass


def _acquire_single_instance_mutex(window_title: str) -> int | None:
    """Prevent duplicate WireDeck GUI windows; focus existing window if already running."""
    if os.name != "nt":
        return None
    ERROR_ALREADY_EXISTS = 183
    SW_RESTORE = 9
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, "Local\\WireDeck_SingleInstance_v1")
        if not handle:
            return None
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, window_title)
            if hwnd:
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetForegroundWindow(hwnd)
            kernel32.CloseHandle(handle)
            return -1
        return int(handle)
    except Exception:
        return None


def _run_quick_play(
    api: WireDeckApi,
    *,
    preset_id: str | None = None,
    transport: str | None = None,
    submode: str | None = None,
    launch_fn: Callable[..., ActionResult] = launch_stream_client,
) -> tuple[bool, str]:
    """Activate the target transport if idle, then launch the preset stream client."""
    if preset_id:
        api.cfg.switch_preset(preset_id)
    if transport in ("wired", "wireless"):
        api.cfg.active_transport = transport
    if submode in ("hotspot", "lan"):
        api.cfg.wireless_submode = submode
    save_config(api.paths, api.cfg)

    active_t = api.cfg.active_transport
    active_sub = api.cfg.wireless_submode
    fallback_view = "wireless" if active_t == "wireless" else "home"

    if active_t == "wireless":
        w_snap = api._wireless_snapshot_fn(api.cfg, probe_network=False)
        w_rep = classify_wireless_snapshot(w_snap, api.cfg)
        if active_sub == "hotspot":
            w3 = next((l for l in w_rep.layers if l.layer == "W3"), None)
            hs_on = bool(w3 and w3.metrics.get("hotspot_state") == "On")
            if not hs_on:
                res = api._execute_or_elevate("enable-hotspot", {"force": False})
                if not res.ok:
                    return False, fallback_view
        else:
            w1 = next((l for l in w_rep.layers if l.layer == "W1"), None)
            w2 = next((l for l in w_rep.layers if l.layer == "W2"), None)
            w4 = next((l for l in w_rep.layers if l.layer == "W4"), None)
            if w2 and w2.status == Severity.FAIL:
                return False, fallback_view
            anti_on = bool(w4 and w4.metrics.get("anti_stutter_active"))
            if not anti_on:
                if w1 and w1.status == Severity.FAIL:
                    return False, fallback_view
                res = api._execute_or_elevate("optimize-wifi", {})
                if not res.ok:
                    return False, fallback_view
    else:
        snap = api._snapshot_fn(api.cfg, probe_network=False)
        rep = classify_snapshot(snap, api.cfg)
        l4 = next((l for l in rep.layers if l.layer == "L4"), None)
        nat_on = bool(l4 and l4.metrics.get("nat_active"))
        if not nat_on:
            if not rep.can_enable_direct:
                return False, fallback_view
            res = api._execute_or_elevate("enable-direct", {})
            if not res.ok:
                return False, fallback_view

    preset = get_preset(api.cfg.active_preset)
    if preset.uses_stream_client:
        custom = api.cfg.custom_client_path.strip()
        if launch_fn is launch_stream_client or custom:
            launch_res = launch_fn(preset_id=preset.id, custom_path=custom) if launch_fn is launch_stream_client else launch_fn(preset_id=preset.id)
        else:
            launch_res = launch_fn(preset_id=preset.id)
        return (bool(launch_res.ok), "" if launch_res.ok else fallback_view)
    return False, fallback_view


def _run_quick_restore(api: WireDeckApi) -> tuple[bool, str]:
    """Restore active wired WinNAT, 5G hotspot, and Wi-Fi anti-stutter tuning back to daily normal."""
    snap = api._snapshot_fn(api.cfg, probe_network=False)
    rep = classify_snapshot(snap, api.cfg)
    l4 = next((l for l in rep.layers if l.layer == "L4"), None)
    if l4 and l4.metrics.get("nat_active"):
        res = api._execute_or_elevate("disable-direct", {})
        if not res.ok:
            return False, "home"

    w_snap = api._wireless_snapshot_fn(api.cfg, probe_network=False)
    hs_on = str((w_snap.get("hotspot") or {}).get("state", "")).lower() == "on"
    if hs_on:
        res = api._execute_or_elevate("disable-hotspot", {})
        if not res.ok:
            return False, "wireless"

    if (api.paths.runtime_dir / "wifi_backup.json").is_file():
        res = api._execute_or_elevate("restore-wifi", {})
        if not res.ok:
            return False, "wireless"

    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="WireDeck", add_help=True)
    parser.add_argument(
        "--preview-state",
        choices=["ready", "breakpoint", "wizard", "ai", "settings", "onboarding", "wireless", "wireless_lan", "wireless_wizard"],
        default=None,
        help="Open GUI in deterministic read-only preview mode",
    )
    parser.add_argument(
        "--preset",
        default=None,
        help="Override active preset in preview or quick-play mode",
    )
    parser.add_argument(
        "--theme",
        choices=["light", "dark"],
        default=None,
        help="Override theme in preview mode",
    )
    parser.add_argument(
        "--lang",
        choices=["zh-CN", "zh-TW", "en", "ja"],
        default=None,
        help="Override language in preview mode",
    )
    parser.add_argument(
        "--capture-to",
        default=None,
        help="Optional PNG output path; captures window screenshot and exits cleanly",
    )
    parser.add_argument(
        "--capture-delay",
        type=float,
        default=2.0,
        help="Seconds to wait before capturing screenshot",
    )
    parser.add_argument(
        "--wizard-step",
        type=int,
        default=None,
        help="Optional 1-based step index when previewing wizard view",
    )
    parser.add_argument(
        "--quick-play",
        action="store_true",
        help="Activate configured connection mode if idle and launch streaming client",
    )
    parser.add_argument(
        "--quick-restore",
        action="store_true",
        help="Restore active WinNAT sharing, hotspot, and Wi-Fi anti-stutter tuning",
    )
    parser.add_argument(
        "--transport",
        choices=["wired", "wireless"],
        default=None,
        help="Optional transport override for --quick-play",
    )
    parser.add_argument(
        "--submode",
        choices=["hotspot", "lan"],
        default=None,
        help="Optional wireless submode override for --quick-play",
    )
    args = parser.parse_args(argv)

    title = f"{APP_NAME} v{APP_VERSION}"
    api = WireDeckApi(preview_state=args.preview_state, preview_wizard_step=args.wizard_step)

    if args.quick_play and not args.preview_state:
        done, _ = _run_quick_play(
            api,
            preset_id=args.preset,
            transport=args.transport,
            submode=args.submode,
        )
        if done:
            return 0
    elif args.quick_restore and not args.preview_state:
        done, _ = _run_quick_restore(api)
        if done:
            return 0

    mutex_handle: int | None = None
    if not args.preview_state and not args.capture_to:
        mutex_handle = _acquire_single_instance_mutex(title)
        if mutex_handle == -1:
            return 0

    # Enable crisp per-monitor DPI awareness on Windows
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    import webview

    if args.preview_state in ("wireless", "wireless_lan", "wireless_wizard"):
        api.cfg.active_transport = "wireless"
        api.cfg.wireless_submode = "lan" if args.preview_state == "wireless_lan" else "hotspot"
    elif args.preview_state in ("ready", "breakpoint", "wizard"):
        api.cfg.active_transport = "wired"
    if args.preset and not args.quick_play:
        api.cfg.switch_preset(args.preset)
    if args.theme:
        api.cfg.theme = args.theme
    if args.lang:
        api.cfg.language = args.lang
    web_dir = get_web_dir()
    index_url = (web_dir / "index.html").resolve().as_uri()
    bg_hex = "#202020" if api.cfg.theme == "dark" else "#f3f3f3"

    window = webview.create_window(
        title=title,
        url=index_url,
        js_api=api,
        width=840,
        height=520,
        min_size=(760, 480),
        background_color=bg_hex,
        resizable=True,
    )
    api._window = window

    def _on_started() -> None:
        _set_titlebar_theme(title, api.cfg.theme == "dark")
        if args.capture_to:
            out_p = Path(args.capture_to)
            _capture_window_screenshot(title, out_p, delay_sec=max(0.5, args.capture_delay))
            try:
                window.destroy()
            except Exception:
                pass

    def _on_closed() -> None:
        api.shutdown()
        if mutex_handle and mutex_handle > 0 and os.name == "nt":
            try:
                ctypes.windll.kernel32.CloseHandle(mutex_handle)
            except Exception:
                pass

    window.events.closed += _on_closed
    icon_file = get_assets_dir() / "wiredeck.ico"
    start_kwargs: dict[str, Any] = {"gui": "edgechromium", "debug": False}
    if icon_file.is_file():
        start_kwargs["icon"] = str(icon_file)

    webview.start(_on_started, **start_kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
