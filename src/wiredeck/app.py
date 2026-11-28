"""GUI entrypoint (`WireDeck.exe`) using Edge WebView2 via pywebview."""

from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import os
import signal
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
from wiredeck.config import (
    AppConfig,
    VALID_HOTSPOT_BANDS,
    export_config_backup,
    import_config_backup,
    load_config,
    normalize_recorder_settings,
    save_config,
)
from wiredeck.diagnostics import classify_snapshot, run_diagnosis, run_link_benchmark
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
from wiredeck.plugins import (
    DEFAULT_PLUGIN_REGISTRY_URL,
    SUPPORTED_CAPABILITIES,
    SUPPORTED_HOOK_EVENTS,
    SUPPORTED_HOST_COMMANDS,
    emit_plugin_hook,
    fetch_plugin_catalog,
    get_active_plugin_badges,
    has_enabled_hook,
    install_plugin_from_github,
    list_installed_plugins,
    resolve_plugin_dynamic_options,
    run_plugin_action,
    run_plugin_check,
    save_plugin_config,
    set_plugin_enabled,
    uninstall_plugin,
)
from wiredeck.presets import get_preset, list_presets, normalize_preset_id
from wiredeck.probes import _ping_peer, collect_system_snapshot
from wiredeck.ps_runner import is_admin, run_elevated_worker, run_powershell
from wiredeck.recorder import (
    RecorderController,
    capture_screenshot,
    delete_clip_file,
    detect_ffmpeg_video_devices,
    detect_recorders,
    inspect_target_path,
    list_recorded_clips,
    list_storage_drives,
    offload_clips_to_target,
    preview_clip_filenames,
    prune_clips_by_quota,
    rename_clip_with_forced_seq,
    resolve_capture_target,
    resolve_effective_split_policy,
    sync_obs_output_dir,
)
from wiredeck.stream_client import (
    build_play_shortcut_name,
    create_play_shortcut,
    create_restore_shortcut,
    download_and_verify_asset,
    fetch_latest_xstreaming_release,
    launch_stream_client,
    resolve_stream_client_info,
)
from wiredeck.system_env import collect_system_env, collect_system_env_cached, evaluate_system_health
from wiredeck.toolkits import discover_toolkits, run_toolkit
from wiredeck.uninstaller import (
    discover_uninstall_items,
    execute_uninstall_cleanup,
    launch_self_uninstall,
)
from wiredeck.updater import (
    apply_staged_app_update,
    check_all_updates,
    download_and_stage_app_update,
    update_all_plugins as updater_update_all_plugins,
)
from wiredeck.version import APP_NAME, APP_VERSION, NAT_NAME
from wiredeck.win32_helpers import Win32Ops, default_win32_ops
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
        win32_ops: Win32Ops | None = None,
        preview_state: str | None = None,
        preview_wizard_step: int | None = None,
        uninstall_wizard: bool = False,
        from_uninstaller: bool = False,
    ) -> None:
        self.paths = paths or resolve_app_paths()
        self.cfg = load_config(self.paths)
        self._snapshot_fn = snapshot_fn
        self._wireless_snapshot_fn = wireless_snapshot_fn
        self._win32_ops = win32_ops or default_win32_ops()
        self._preview_state = preview_state
        self._preview_wizard_step = preview_wizard_step
        self._uninstall_wizard = bool(uninstall_wizard)
        self._from_uninstaller = bool(from_uninstaller)
        self._uninstall_exit_code = 2 if uninstall_wizard else 0
        self._last_report: DiagnosticReport | None = None
        self._last_wireless_report: DiagnosticReport | None = None
        self._bridge: LocalBridgeServer | None = None
        self._window: Any = None
        self._recorder = RecorderController()
        self._last_update_check: dict[str, Any] | None = None
        self._last_uninstall_scan: dict[str, Any] | None = None
        if preview_state in ("wireless", "wireless_wizard"):
            self.cfg.active_transport = "wireless"
            self.cfg.wireless_submode = "hotspot"
        elif preview_state == "wireless_lan":
            self.cfg.active_transport = "wireless"
            self.cfg.wireless_submode = "lan"
        elif preview_state in ("ready", "breakpoint", "wizard"):
            self.cfg.active_transport = "wired"

    def _preview_view_name(self) -> str | None:
        if self._preview_state in ("wizard", "ai", "settings", "plugins", "recorder"):
            return self._preview_state
        if self._preview_state in ("wireless", "wireless_lan"):
            return "wireless"
        if self._preview_state == "wireless_wizard":
            return "wizard"
        if self._preview_state == "update":
            return "settings"
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
            else (False if (self._preview_state or self._uninstall_wizard) else (not self.cfg.onboarding_completed))
        )
        return {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "is_admin": is_admin(),
            "is_portable": self.paths.is_portable,
            "config": self.cfg.to_dict(),
            "active_preset": active_preset.to_dict(),
            "presets_meta": [p.to_dict() for p in list_presets()],
            "show_onboarding": show_onboarding,
            "paths": self.paths.to_dict(),
            "bridge": bridge_info,
            "preview_state": self._preview_state,
            "preview_view": self._preview_view_name(),
            "preview_wizard_step": self._preview_wizard_step,
            "uninstall_wizard_mode": self._uninstall_wizard or (self._preview_state == "uninstall"),
            "from_uninstaller": self._from_uninstaller,
            "can_self_uninstall": bool((self.paths.exe_dir / "unins000.exe").is_file() or self.paths.is_portable),
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
        ctx = _build_hook_context(self.cfg)
        ctx["action"] = action
        if action in ("enable-direct", "enable-hotspot", "optimize-wifi"):
            emit_plugin_hook(self.paths, "before_transport_enable", ctx, win32_ops=self._win32_ops)
        elif action in ("disable-direct", "disable-hotspot", "restore-wifi"):
            emit_plugin_hook(self.paths, "before_transport_disable", ctx, win32_ops=self._win32_ops)

        res = self._execute_or_elevate(action, opts or {})

        if res.ok and action in ("enable-direct", "enable-hotspot", "optimize-wifi"):
            emit_plugin_hook(self.paths, "after_transport_enable", ctx, win32_ops=self._win32_ops)
        elif res.ok and action in ("disable-direct", "disable-hotspot", "restore-wifi"):
            emit_plugin_hook(self.paths, "after_transport_disable", ctx, win32_ops=self._win32_ops)
        return res.to_dict()

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
        if "hotspot_band" in patch:
            hb = str(patch["hotspot_band"] or "").strip().lower()
            if hb in VALID_HOTSPOT_BANDS:
                self.cfg.hotspot_band = hb
                self.cfg.hotspot_prefer_5ghz = hb == "5ghz"
        elif "hotspot_prefer_5ghz" in patch:
            self.cfg.hotspot_prefer_5ghz = bool(patch["hotspot_prefer_5ghz"])
            self.cfg.hotspot_band = "5ghz" if self.cfg.hotspot_prefer_5ghz else "auto"
        if "onboarding_completed" in patch:
            self.cfg.onboarding_completed = bool(patch["onboarding_completed"])
        if "keep_awake_during_stream" in patch:
            self.cfg.keep_awake_during_stream = bool(patch["keep_awake_during_stream"])
        if "auto_restore_on_exit" in patch:
            self.cfg.auto_restore_on_exit = bool(patch["auto_restore_on_exit"])
        if "update_channel" in patch:
            uc = str(patch["update_channel"] or "stable").strip().lower()
            if uc in ("stable", "beta"):
                self.cfg.update_channel = uc
        if "update_feed_url" in patch:
            self.cfg.update_feed_url = str(patch["update_feed_url"] or "").strip()
        if "auto_check_updates" in patch:
            self.cfg.auto_check_updates = bool(patch["auto_check_updates"])
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
        bundle_path, prompt = export_diagnostic_bundle(
            self.paths,
            report,
            self.cfg,
            system_health=self.get_system_health(),
        )
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
        done, fallback_view, err_msg, launch_details = _run_quick_play_detailed(
            self,
            preset_id=self.cfg.active_preset,
            transport=self.cfg.active_transport,
            submode=self.cfg.wireless_submode,
            silent=False,
            win32_ops=self._win32_ops,
        )
        if done:
            client_pid = int((launch_details or {}).get("pid") or 0)
            self._start_session_supervisor(client_pid, launch_details or {}, blocking=False)
        return {
            "ok": done,
            "code": "QUICK_PLAY_OK" if done else "QUICK_PLAY_BLOCKED",
            "fallback_view": fallback_view,
            "message": "" if done else (err_msg or "存在前置阻断或未找到客户端，已定位到需要处理的步骤"),
        }

    def _make_recorder_callback(self) -> Callable[[str], dict[str, Any]]:
        def _cb(action_name: str) -> dict[str, Any]:
            act = str(action_name or "").strip().lower()
            if act == "start":
                return self.recorder_start()
            if act == "stop":
                return self.recorder_stop()
            if act == "save_replay":
                return self.recorder_save_replay()
            if act == "offload":
                return self.recorder_offload_now()
            if act == "split_now":
                return self.recorder_split_now()
            return {"ok": False, "code": "UNKNOWN_RECORDER_ACTION", "message": f"不支持的录屏动作: {act}"}

        return _cb

    def _start_session_supervisor(
        self,
        client_pid: int,
        launch_details: dict[str, Any],
        *,
        skip_plugins: bool = False,
        plugin_overrides: dict[str, dict[str, Any]] | None = None,
        blocking: bool = False,
    ) -> None:
        need_exit_hook = (not skip_plugins) and has_enabled_hook(self.paths, "on_client_exit")
        need_awake = bool(self.cfg.keep_awake_during_stream)
        need_rec = bool(self.cfg.recorder.auto_link_quick_play)
        need_restore = bool(self.cfg.auto_restore_on_exit)
        if not blocking and (client_pid <= 0 or not (need_exit_hook or need_awake or need_rec or need_restore)):
            return

        if need_rec:
            self._recorder.on_session_start(
                self.cfg,
                self.paths,
                system_env=collect_system_env_cached(),
                win32_ops=self._win32_ops,
            )

        if client_pid <= 0 or not (need_exit_hook or need_awake or need_rec or need_restore):
            return

        def _watch() -> None:
            if need_awake:
                self._win32_ops.set_keep_awake(True)
            try:
                self._win32_ops.wait_for_pid_exit(client_pid, poll_interval=1.0)
            finally:
                if need_awake:
                    self._win32_ops.set_keep_awake(False)
                if need_rec:
                    self._recorder.on_session_end(self.cfg, self.paths, win32_ops=self._win32_ops)
                if need_exit_hook:
                    exit_ctx = _build_hook_context(
                        self.cfg,
                        silent=blocking,
                        paths=self.paths,
                        plugin_overrides=plugin_overrides,
                        extra={
                            "client_pid": client_pid,
                            "client_exe": str(launch_details.get("exe_name") or ""),
                            "client_exe_path": str(launch_details.get("path") or ""),
                            "client_name": str(launch_details.get("client_name") or ""),
                            "_recorder_callback": self._make_recorder_callback(),
                        },
                    )
                    emit_plugin_hook(self.paths, "on_client_exit", exit_ctx, win32_ops=self._win32_ops)
                if need_restore:
                    try:
                        _run_quick_restore(self, silent=True)
                    except Exception:
                        pass

        if blocking:
            _watch()
        else:
            threading.Thread(target=_watch, name="WireDeck-SessionSupervisor", daemon=True).start()

    def create_play_shortcut(self) -> dict[str, Any]:
        """Create a silent Desktop .lnk with the target app's name & icon."""
        if self._preview_state:
            client_info = {
                "name": "XStreaming",
                "exe_path": "C:\\Program Files\\XStreaming\\XStreaming.exe",
                "icon_location": "C:\\Program Files\\XStreaming\\XStreaming.exe,0",
                "is_custom": False,
            }
            fname = build_play_shortcut_name(self.cfg, client_info=client_info)
            return ActionResult(
                ok=True,
                code="PREVIEW_ONLY",
                message=f"预览模式下模拟已创建：{fname}",
                details={"filename": fname, "client_name": "XStreaming", "silent": True},
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

    def _preview_plugins_data(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        preview_manifest = {
            "schema": "wiredeck-plugin/v2",
            "id": "framegen-sharpen",
            "name": "高刷补帧与画质锐化（FrameGen & Sharpen）",
            "version": "1.1.0",
            "author": "WireDeck Official",
            "category": "video-enhance",
            "description": "实时读取小黄鸭 Settings.xml 配置与热键，支持选定串流专属配置、智能防重触发、自动拉起 LSFG 补帧与 LS1 / FSR 画质锐化。",
            "homepage": "https://github.com/Iraryi/WireDeck/tree/main/registry/plugins/framegen-sharpen",
            "capabilities": ["video-enhance", "automation"],
            "preset_scope": ["all"],
            "enabled_by_default": True,
            "requires_admin": False,
            "timeout_sec": 12,
            "hooks": {
                "before_client_launch": "hooks/before_launch.ps1",
                "after_client_launch": "hooks/after_launch.ps1",
                "on_client_exit": "hooks/on_exit.ps1",
            },
            "actions": [
                {"id": "detect_engine", "label": "自动扫描小黄鸭 / Magpie", "script": "actions/detect_engine.ps1", "description": "从 Steam 库或本地目录自动查找安装路径", "style": "default"},
                {"id": "create_streaming_profile", "label": "生成推荐串流配置", "script": "actions/create_streaming_profile.ps1", "description": "备份并写入 WGC + LSFG3 2x + LS1 配置到小黄鸭 Settings.xml", "style": "default"},
                {"id": "open_engine", "label": "打开参数面板", "script": "actions/open_engine.ps1", "description": "打开 Lossless Scaling 或 Magpie 设置界面", "style": "default"},
                {"id": "trigger_now", "label": "测试补帧热键", "script": "actions/trigger_now.ps1", "description": "1.5 秒后发送配置好的快捷键", "style": "primary"},
            ],
            "settings_schema": [
                {
                    "key": "engine",
                    "type": "select",
                    "label": "增强引擎",
                    "default": "lossless_scaling",
                    "options": [
                        {"value": "lossless_scaling", "label": "Lossless Scaling（小黄鸭 LSFG / LS1）"},
                        {"value": "magpie", "label": "Magpie（开源 FSR / Anime4K 免费方案）"},
                    ],
                },
                {
                    "key": "ls_profile",
                    "type": "dynamic_select",
                    "label": "小黄鸭配置（Settings.xml）",
                    "default": "__auto__",
                    "options_script": "actions/list_profiles.ps1",
                },
                {"key": "exe_path", "type": "file_path", "label": "程序路径（.exe）", "default": "D:\\SteamLibrary\\steamapps\\common\\Lossless Scaling\\LosslessScaling.exe", "placeholder": "点击自动扫描或手动指定 .exe"},
                {
                    "key": "trigger_mode",
                    "type": "select",
                    "label": "自动触发策略",
                    "default": "smart",
                    "options": [
                        {"value": "smart", "label": "智能防重（若小黄鸭已开 AutoScale 则跳过热键）"},
                        {"value": "always_hotkey", "label": "始终发送热键触发"},
                        {"value": "launch_only", "label": "仅后台拉起增强器"},
                    ],
                },
                {"key": "auto_launch_engine", "type": "bool", "label": "开玩时自动后台启动", "default": True},
                {"key": "auto_trigger_scale", "type": "bool", "label": "窗口就绪后自动触发补帧/锐化", "default": True},
                {"key": "scale_hotkey", "type": "hotkey", "label": "触发快捷键（留空自动读取 XML）", "default": "Ctrl+Alt+S"},
                {"key": "delay_sec", "type": "number", "label": "窗口就绪后等待（秒）", "default": 2.0, "min": 0.5, "max": 15.0, "step": 0.5},
                {"key": "keep_awake", "type": "bool", "label": "串流期间防止息屏休眠", "default": True},
                {"key": "auto_close_on_exit", "type": "bool", "label": "退出串流时关闭增强器", "default": False},
            ],
            "requirements": {
                "summary": "搭配 Steam 版 Lossless Scaling（小黄鸭）或开源免费 Magpie 使用",
                "install_hint": "Steam 搜索 Lossless Scaling 或前往 GitHub 获取 Magpie",
            },
        }
        installed = [
            {
                "valid": True,
                "errors": [],
                "enabled": True,
                "settings": {
                    "engine": "lossless_scaling",
                    "ls_profile": "WireDeck 串流高刷推荐",
                    "exe_path": "D:\\SteamLibrary\\steamapps\\common\\Lossless Scaling\\LosslessScaling.exe",
                    "trigger_mode": "smart",
                    "auto_launch_engine": True,
                    "auto_trigger_scale": True,
                    "scale_hotkey": "",
                    "delay_sec": 2.0,
                    "keep_awake": True,
                    "auto_close_on_exit": False,
                },
                "install_meta": {"version": "1.1.0", "verified_sha256": True},
                "manifest": preview_manifest,
            }
        ]
        catalog = {
            "ok": True,
            "registry_url": self.cfg.plugin_registry_url or DEFAULT_PLUGIN_REGISTRY_URL,
            "plugins": [
                {
                    "id": "framegen-sharpen",
                    "name": preview_manifest["name"],
                    "version": "1.1.0",
                    "author": "WireDeck Official",
                    "category": "video-enhance",
                    "capabilities": ["video-enhance", "automation"],
                    "description": preview_manifest["description"],
                    "homepage": preview_manifest["homepage"],
                    "requirements": preview_manifest["requirements"],
                },
                {
                    "id": "nvidia-driver-toolkit",
                    "name": "NVIDIA 驱动更新与双显卡调度工具包",
                    "version": "1.0.0",
                    "author": "WireDeck Official",
                    "category": "video-enhance",
                    "capabilities": ["video-enhance", "automation"],
                    "description": "自动识别笔记本 NVIDIA 显卡型号、在线检索并拉取官方最新 Game Ready / Studio 驱动安装包，支持串流端与小黄鸭独显性能绑定及 Smooth Motion 引导。",
                    "homepage": "https://github.com/Iraryi/WireDeck/tree/main/registry/plugins/nvidia-driver-toolkit",
                    "requirements": {"summary": "适用于 NVIDIA GeForce RTX / GTX 显卡（RTX 40/50 系支持驱动级 Smooth Motion）"},
                },
            ],
            "error": "",
        }
        return installed, catalog

    def list_plugins(self) -> list[dict[str, Any]]:
        if self._preview_state == "plugins":
            installed, _ = self._preview_plugins_data()
            return installed
        return list_installed_plugins(self.paths)

    def fetch_plugin_catalog(self, custom_url: str = "") -> dict[str, Any]:
        if self._preview_state == "plugins":
            _, catalog = self._preview_plugins_data()
            return catalog
        reg = (custom_url or "").strip() or self.cfg.plugin_registry_url
        return fetch_plugin_catalog(reg)

    def install_plugin(self, plugin_id: str = "", direct_url: str = "") -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下模拟已从 GitHub 校验安装").to_dict()
        return install_plugin_from_github(
            self.paths,
            plugin_id=plugin_id,
            registry_url=self.cfg.plugin_registry_url,
            direct_url=direct_url,
        ).to_dict()

    def uninstall_plugin(self, plugin_id: str, remove_config: bool = False) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下模拟已卸载插件").to_dict()
        return uninstall_plugin(self.paths, plugin_id, remove_config=bool(remove_config)).to_dict()

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式切换插件状态").to_dict()
        return set_plugin_enabled(self.paths, plugin_id, bool(enabled)).to_dict()

    def save_plugin_settings(self, plugin_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        if self._preview_state:
            return {"ok": True, "code": "PREVIEW_ONLY", "message": "预览模式已应用设置"}
        saved = save_plugin_config(self.paths, plugin_id, settings=settings)
        return {"ok": True, "code": "PLUGIN_SETTINGS_SAVED", "message": "插件设置已保存", **saved}

    def run_plugin_action(self, plugin_id: str, action_id: str) -> dict[str, Any]:
        if self._preview_state:
            return {"ok": True, "code": "PREVIEW_ONLY", "message": f"预览模式已触发 {action_id}"}
        return run_plugin_action(
            self.paths,
            plugin_id,
            action_id,
            context=_build_hook_context(
                self.cfg,
                paths=self.paths,
                extra={"_recorder_callback": self._make_recorder_callback()},
            ),
            win32_ops=self._win32_ops,
        )

    def run_plugin_check(self, plugin_id: str) -> dict[str, Any]:
        if self._preview_state:
            return {"ok": True, "status": "ok", "code": "PREVIEW_OK", "message": "预览模式：RTX 5070 Ti (240 Hz) · 已读取小黄鸭配置「WireDeck 串流高刷推荐」与热键 Ctrl+Alt+S"}
        return run_plugin_check(self.paths, plugin_id, context=_build_hook_context(self.cfg, paths=self.paths))

    def get_plugin_dynamic_options(self, plugin_id: str) -> dict[str, Any]:
        if self._preview_state:
            return {
                "ok": True,
                "options_by_key": {
                    "ls_profile": [
                        {"value": "__auto__", "label": "自动匹配（使用小黄鸭自身规则）"},
                        {"value": "WireDeck 串流高刷推荐", "label": "WireDeck 串流高刷推荐 [LSFG3 240Hz · LS1 · WGC]"},
                        {"value": "Fullscreen", "label": "Fullscreen [Off · Integer · DXGI]"},
                    ]
                },
                "meta_by_key": {"ls_profile": {"detected_hotkey": "Ctrl+Alt+S"}},
            }
        return resolve_plugin_dynamic_options(self.paths, plugin_id, context=_build_hook_context(self.cfg, paths=self.paths))

    def get_system_env(self) -> dict[str, Any]:
        if self._preview_state:
            return {
                "gpus": [
                    {"name": "Intel(R) UHD Graphics", "vendor": "Intel", "driver_version": "32.0.101.6733", "is_nvidia": False, "is_discrete": False, "is_laptop": True, "supports_smooth_motion": False, "refresh_hz": 240, "resolution": "2560x1600"},
                    {"name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU", "vendor": "NVIDIA", "driver_version": "572.84", "is_nvidia": True, "is_discrete": True, "is_laptop": True, "supports_smooth_motion": True, "refresh_hz": 0, "resolution": ""},
                ],
                "primary_nvidia_gpu": {"name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU", "driver_version": "572.84", "supports_smooth_motion": True},
                "is_dual_gpu": True,
                "max_refresh_hz": 240,
                "primary_resolution": "2560x1600",
                "virtual_displays": [],
                "on_battery": False,
                "nvidia_app_installed": True,
            }
        return collect_system_env_cached()

    def get_system_health(self) -> dict[str, Any]:
        if self._preview_state:
            rec_st = self._preview_recorder_state()
            return evaluate_system_health(
                system_env=self.get_system_env(),
                cfg_dict=self.cfg.to_dict(),
                recorder_detected=rec_st["detected"],
                target_status=rec_st["target_status"],
                effective_split=rec_st["effective_split"],
                fallback_pending_count=0,
                installed_plugins=self.list_plugins(),
            )
        rec = self.cfg.recorder
        sys_env = collect_system_env_cached()
        drives = list_storage_drives(bitrate_mbps=rec.ffmpeg_bitrate_mbps)
        t_dir = rec.output_dir.strip()
        det = detect_recorders(self.cfg, system_env=sys_env)
        tgt = inspect_target_path(t_dir, bitrate_mbps=rec.ffmpeg_bitrate_mbps, min_free_gb=rec.min_free_gb, drives=drives)
        eff = resolve_effective_split_policy(
            rec.split_mode,
            rec.split_minutes,
            rec.split_size_mb,
            filesystem=str(tgt.get("filesystem") or ""),
        )
        fb_dir = rec.fallback_dir.strip()
        fb_pending = 0
        if fb_dir and fb_dir != t_dir and Path(fb_dir).is_dir():
            fb_pending = int(list_recorded_clips(fb_dir).get("total_count") or 0)
        w_rep = self._last_wireless_report
        band_24g = False
        if self.cfg.active_transport == "wireless" and w_rep is not None:
            w2 = next((l for l in w_rep.layers if l.layer == "W2"), None)
            w3 = next((l for l in w_rep.layers if l.layer == "W3"), None)
            band_24g = bool(
                (w2 and "2.4" in str(w2.metrics.get("band") or ""))
                or (w3 and "two" in str(w3.metrics.get("hotspot_band") or "").lower())
            )
        return evaluate_system_health(
            system_env=sys_env,
            cfg_dict=self.cfg.to_dict(),
            recorder_detected=det,
            target_status=tgt,
            effective_split=eff,
            fallback_pending_count=fb_pending,
            installed_plugins=list_installed_plugins(self.paths),
            wifi_band_24g=band_24g,
        )

    def pick_plugin_file(self, file_filter: str = "") -> dict[str, Any]:
        if self._preview_state:
            return {"ok": True, "path": "D:\\SteamLibrary\\steamapps\\common\\Lossless Scaling\\LosslessScaling.exe"}
        if self._window is None:
            return {"ok": False, "code": "NO_WINDOW"}
        try:
            import webview

            ft = (file_filter.strip(), "All files (*.*)") if file_filter.strip() else ("Executable (*.exe)", "All files (*.*)")
            picked = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=ft)
            if not picked:
                return {"ok": False, "code": "CANCELLED"}
            chosen = str(picked[0] if isinstance(picked, (list, tuple)) else picked).strip()
            return {"ok": True, "path": chosen} if chosen else {"ok": False, "code": "CANCELLED"}
        except Exception as exc:
            return {"ok": False, "code": "PICK_FAILED", "message": str(exc)}

    def set_plugin_registry_url(self, url: str = "") -> dict[str, Any]:
        if self._preview_state:
            return {"ok": True, "registry_url": url.strip() or DEFAULT_PLUGIN_REGISTRY_URL}
        self.cfg.plugin_registry_url = (url or "").strip()
        save_config(self.paths, self.cfg)
        return {"ok": True, "registry_url": self.cfg.plugin_registry_url or DEFAULT_PLUGIN_REGISTRY_URL}

    def get_plugin_badges(self) -> list[dict[str, Any]]:
        if self._preview_state == "plugins":
            installed, _ = self._preview_plugins_data()
            return [
                {
                    "id": p["manifest"]["id"],
                    "name": p["manifest"]["name"],
                    "version": p["manifest"]["version"],
                    "category": p["manifest"]["category"],
                }
                for p in installed
            ]
        if self._preview_state:
            return []
        return get_active_plugin_badges(self.paths, self.cfg.active_preset)

    def get_plugin_sdk_info(self) -> dict[str, Any]:
        return {
            "schema": "wiredeck-plugin/v2",
            "default_registry_url": DEFAULT_PLUGIN_REGISTRY_URL,
            "active_registry_url": self.cfg.plugin_registry_url or DEFAULT_PLUGIN_REGISTRY_URL,
            "hooks": list(SUPPORTED_HOOK_EVENTS),
            "capabilities": list(SUPPORTED_CAPABILITIES),
            "host_commands": list(SUPPORTED_HOST_COMMANDS),
        }

    def _preview_recorder_state(self) -> dict[str, Any]:
        sample_rec = normalize_recorder_settings(
            {
                "backend": "obs",
                "mode": "replay",
                "auto_link_quick_play": True,
                "output_dir": "E:\\XboxClips",
                "auto_offload": True,
                "ffmpeg_format": "mkv",
                "ffmpeg_encoder": "auto",
                "ffmpeg_bitrate_mbps": 10,
                "ffmpeg_fps": 60,
                "max_storage_gb": 80,
                "min_free_gb": 5,
                "auto_prune": False,
                "split_mode": "both",
                "split_minutes": 30,
                "split_size_mb": 2048,
                "filename_template": "{preset}_{date}_{time}",
                "apply_naming_on_offload": True,
            }
        ).to_dict()
        drives = [
            {
                "device_id": "E:",
                "volume_name": "USB_1TB",
                "drive_type": 2,
                "drive_type_label": "USB 可移动盘",
                "filesystem": "exFAT",
                "free_bytes": 116_400_000_000,
                "total_bytes": 1_000_000_000_000,
                "free_gb": 108.4,
                "total_gb": 931.5,
                "supports_large_files": True,
                "est_hours": 24.1,
                "is_removable": True,
                "recommended": True,
            },
            {
                "device_id": "D:",
                "volume_name": "Data",
                "drive_type": 3,
                "drive_type_label": "本机固定盘",
                "filesystem": "NTFS",
                "free_bytes": 134_000_000_000,
                "total_bytes": 671_000_000_000,
                "free_gb": 124.8,
                "total_gb": 625.0,
                "supports_large_files": True,
                "est_hours": 27.7,
                "is_removable": False,
                "recommended": False,
            },
        ]
        return {
            "ok": True,
            "recorder": sample_rec,
            "detected": {
                "effective_backend": "obs",
                "obs": {
                    "installed": True,
                    "exe_path": "C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe",
                    "profile_name": "WireDeck",
                    "record_format": "mkv",
                    "record_path": "E:\\XboxClips",
                    "split_enabled": True,
                    "split_type": "Time",
                    "split_time": 30,
                    "split_size": 2048,
                },
                "nvidia": {"available": True, "supports_av1": True, "gpu_name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU"},
                "gamebar": {"available": True},
                "ffmpeg": {"available": True, "exe_path": "C:\\ffmpeg\\bin\\ffmpeg.exe"},
                "custom": {"available": False, "exe_path": ""},
                "watch_dirs": ["C:\\Users\\Demo\\Videos\\Captures", "C:\\Users\\Demo\\Videos\\NVIDIA"],
            },
            "drives": drives,
            "target_status": {
                "configured": True,
                "online": True,
                "path": "E:\\XboxClips",
                "device_id": "E:",
                "volume_name": "USB_1TB",
                "filesystem": "exFAT",
                "supports_large_files": True,
                "is_removable": True,
                "free_gb": 108.4,
                "est_hours": 24.1,
                "warnings": [],
            },
            "fallback_status": {"configured": False, "online": False},
            "fallback_pending_count": 0,
            "effective_split": resolve_effective_split_policy("both", 30, 2048, filesystem="exFAT"),
            "filename_preview": preview_clip_filenames(
                "{preset}_{date}_{time}",
                ext="mkv",
                preset="xbox",
                backend="obs",
                mode="replay",
            ),
            "runtime_status": {
                "is_recording": False,
                "active_backend": "",
                "active_file": "",
                "current_segment": 0,
                "segments_completed": 0,
                "elapsed_sec": 0.0,
                "segment_elapsed_sec": 0.0,
                "current_size_mb": 0.0,
                "used_fallback": False,
            },
            "is_recording": False,
            "active_file": "",
            "capture_target": {
                "scope": "stream_window",
                "effective_scope": "window",
                "ready": True,
                "fell_back_to_desktop": False,
                "hwnd": 1001,
                "window_title": "XStreaming",
                "window_exe": "XStreaming.exe",
                "window_class": "Chrome_WidgetWin_1",
                "is_minimized": False,
                "video_device": "",
                "label": "XStreaming",
                "warning": "",
            },
            "open_windows": [
                {"hwnd": 1001, "title": "XStreaming", "process_name": "XStreaming.exe", "class_name": "Chrome_WidgetWin_1", "is_minimized": False, "width": 1920, "height": 1080},
                {"hwnd": 1002, "title": "Chiaki-ng", "process_name": "chiaki-ng.exe", "class_name": "Qt6QWindowIcon", "is_minimized": False, "width": 1280, "height": 720},
            ],
            "video_devices": [
                {"name": "USB3.0 HD Video Capture", "is_capture_card": True},
            ],
            "clips_summary": {
                "ok": True,
                "total_count": 2,
                "total_gb": 1.84,
                "clips": [
                    {"name": "1_xbox_20261127_221000.mkv", "path": "E:\\XboxClips\\1_xbox_20261127_221000.mkv", "ext": "mkv", "seq": 1, "size_mb": 1280.4, "mtime_iso": "2026-11-27 22:10"},
                    {"name": "2_xbox_20261127_224000.mp4", "path": "E:\\XboxClips\\2_xbox_20261127_224000.mp4", "ext": "mp4", "seq": 2, "size_mb": 604.2, "mtime_iso": "2026-11-27 22:40"},
                ],
            },
        }

    def get_recorder_state(self) -> dict[str, Any]:
        if self._preview_state == "recorder":
            return self._preview_recorder_state()
        sys_env = collect_system_env_cached() if not self._preview_state else {}
        rec = self.cfg.recorder
        drives = list_storage_drives(bitrate_mbps=rec.ffmpeg_bitrate_mbps)
        t_dir = rec.output_dir.strip()
        fb_dir = rec.fallback_dir.strip()
        detected = detect_recorders(self.cfg, system_env=sys_env)
        target_status = inspect_target_path(
            t_dir,
            bitrate_mbps=rec.ffmpeg_bitrate_mbps,
            min_free_gb=rec.min_free_gb,
            drives=drives,
        )
        fb_status = (
            inspect_target_path(fb_dir, bitrate_mbps=rec.ffmpeg_bitrate_mbps, min_free_gb=rec.min_free_gb, drives=drives)
            if fb_dir
            else {"configured": False, "online": False}
        )
        fb_pending = 0
        if fb_dir and fb_dir != t_dir and fb_status.get("online"):
            fb_pending = int(list_recorded_clips(fb_dir).get("total_count") or 0)

        preview_ext = rec.ffmpeg_format if detected["effective_backend"] == "ffmpeg" else (
            (detected.get("obs") or {}).get("record_format") or rec.ffmpeg_format or "mkv"
        )
        ff_exe = str((detected.get("ffmpeg") or {}).get("exe_path") or "")
        open_wins = self._win32_ops.list_windows() if self._win32_ops.list_windows else []
        vid_devs = detect_ffmpeg_video_devices(ff_exe) if (ff_exe and rec.capture_scope == "capture_card") else []
        cap_tgt = resolve_capture_target(
            self.cfg,
            windows=open_wins,
            video_devices=vid_devs if rec.capture_scope == "capture_card" else None,
            win32_ops=self._win32_ops,
        )
        return {
            "ok": True,
            "recorder": rec.to_dict(),
            "detected": detected,
            "drives": drives,
            "target_status": target_status,
            "fallback_status": fb_status,
            "fallback_pending_count": fb_pending,
            "effective_split": resolve_effective_split_policy(
                rec.split_mode,
                rec.split_minutes,
                rec.split_size_mb,
                filesystem=str(target_status.get("filesystem") or ""),
            ),
            "filename_preview": preview_clip_filenames(
                rec.filename_template,
                target_dir=t_dir,
                ext=preview_ext,
                preset=self.cfg.active_preset,
                backend=detected["effective_backend"],
                mode=rec.mode,
            ),
            "runtime_status": self._recorder.get_runtime_status(),
            "is_recording": self._recorder.is_recording,
            "active_file": self._recorder.active_file,
            "capture_target": cap_tgt,
            "open_windows": open_wins[:40],
            "video_devices": vid_devs,
            "clips_summary": list_recorded_clips(t_dir),
        }

    def list_capture_sources(self) -> dict[str, Any]:
        """Refresh live open desktop windows & DirectShow video capture devices."""
        if self._preview_state == "recorder":
            st = self._preview_recorder_state()
            return {
                "ok": True,
                "open_windows": st["open_windows"],
                "video_devices": st["video_devices"],
                "capture_target": st["capture_target"],
            }
        rec = self.cfg.recorder
        det = detect_recorders(self.cfg, system_env=collect_system_env_cached())
        ff_exe = str((det.get("ffmpeg") or {}).get("exe_path") or "")
        wins = self._win32_ops.list_windows() if self._win32_ops.list_windows else []
        vdevs = detect_ffmpeg_video_devices(ff_exe) if ff_exe else []
        tgt = resolve_capture_target(self.cfg, windows=wins, video_devices=vdevs, win32_ops=self._win32_ops)
        return {
            "ok": True,
            "open_windows": wins[:50],
            "video_devices": vdevs,
            "capture_target": tgt,
        }

    def save_recorder_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        if self._preview_state:
            return {"ok": True, "code": "PREVIEW_ONLY", "message": "预览模式下已模拟保存录制配置"}
        merged = {**self.cfg.recorder.to_dict(), **(patch if isinstance(patch, dict) else {})}
        self.cfg.recorder = normalize_recorder_settings(merged)
        save_config(self.paths, self.cfg)
        return {"ok": True, "code": "RECORDER_SAVED", "message": "录屏与存储设置已保存", "recorder": self.cfg.recorder.to_dict()}

    def preview_recorder_filename(self, template: str = "", ext: str = "") -> dict[str, Any]:
        rec = self.cfg.recorder
        return {
            "ok": True,
            "examples": preview_clip_filenames(
                template or rec.filename_template,
                target_dir=rec.output_dir.strip(),
                ext=ext or rec.ffmpeg_format or "mkv",
                preset=self.cfg.active_preset,
                backend=rec.backend,
                mode=rec.mode,
            ),
        }

    def list_recorder_clips(self) -> dict[str, Any]:
        if self._preview_state == "recorder":
            return self._preview_recorder_state()["clips_summary"]
        return list_recorded_clips(self.cfg.recorder.output_dir.strip())

    def recorder_start(self) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下已模拟开启录屏 / 回放缓存").to_dict()
        return self._recorder.start_recording(
            self.cfg,
            self.paths,
            system_env=collect_system_env_cached(),
            win32_ops=self._win32_ops,
        ).to_dict()

    def recorder_stop(self) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下已模拟停止录屏").to_dict()
        return self._recorder.stop_recording(self.cfg, self.paths, win32_ops=self._win32_ops).to_dict()

    def recorder_save_replay(self) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下已模拟触发保存高光回放").to_dict()
        return self._recorder.save_replay(
            self.cfg,
            system_env=collect_system_env_cached(),
            win32_ops=self._win32_ops,
        ).to_dict()

    def recorder_split_now(self) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下已模拟切入下一录像段").to_dict()
        return self._recorder.split_now(self.cfg, self.paths, win32_ops=self._win32_ops).to_dict()

    def recorder_take_screenshot(self) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(
                ok=True,
                code="PREVIEW_ONLY",
                message="预览模式下已模拟保存纯净截图：1_xbox_20261128_143500.png",
            ).to_dict()
        return capture_screenshot(self.cfg, win32_ops=self._win32_ops).to_dict()

    def run_link_benchmark(self, transport: str = "") -> dict[str, Any]:
        t_mode = (transport or self.cfg.active_transport or "wired").strip().lower()
        if self._preview_state:
            if t_mode == "wireless":
                return {
                    "ok": True,
                    "target_ip": "192.168.137.42",
                    "transport": "wireless",
                    "grade": "A",
                    "sent": 12,
                    "received": 12,
                    "loss_pct": 0.0,
                    "min_ms": 1.1,
                    "avg_ms": 2.3,
                    "max_ms": 4.2,
                    "jitter_ms": 0.9,
                    "recommended_bitrate": "25–40 Mbps / 60Hz 流畅",
                    "summary": "无线链路稳定：抖动 < 1ms，零丢包，可流畅 60Hz 高码率串流。",
                }
            return {
                "ok": True,
                "target_ip": self.cfg.peer_ip,
                "transport": "wired",
                "grade": "S",
                "sent": 12,
                "received": 12,
                "loss_pct": 0.0,
                "min_ms": 0.3,
                "avg_ms": 0.5,
                "max_ms": 0.9,
                "jitter_ms": 0.2,
                "recommended_bitrate": "50+ Mbps / 120Hz 满帧无损",
                "summary": "极佳电竞级稳态：几乎零抖动零丢包，可拉满高码率与 120Hz 串流。",
            }

        target_ip = self.cfg.peer_ip
        if t_mode == "wireless":
            target_ip = self.cfg.wireless_peer_ip
            if not target_ip and self._last_wireless_report:
                target_ip = str((self._last_wireless_report.snapshot.get("connectivity") or {}).get("target_peer_ip") or "")
        return run_link_benchmark(target_ip, count=12, transport=t_mode)

    def export_config_backup(self) -> dict[str, Any]:
        if self._preview_state:
            return {"ok": True, "path": "D:\\Backups\\WireDeck_Backup.json", "plugin_config_count": 2}
        if self._window is None:
            out_p = self.paths.exports_dir / f"WireDeck_Backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
            return export_config_backup(self.paths, out_p, cfg=self.cfg)
        try:
            import webview

            save_fn = getattr(webview, "SAVE_DIALOG", 30)
            picked = self._window.create_file_dialog(
                save_fn,
                save_filename=f"WireDeck_Backup_{time.strftime('%Y%m%d')}.json",
                file_types=("WireDeck Backup (*.json)",),
            )
            if not picked:
                return {"ok": False, "code": "CANCELLED"}
            chosen = str(picked[0] if isinstance(picked, (list, tuple)) else picked).strip()
            if not chosen:
                return {"ok": False, "code": "CANCELLED"}
            if not chosen.lower().endswith(".json"):
                chosen += ".json"
            return export_config_backup(self.paths, Path(chosen), cfg=self.cfg)
        except Exception as exc:
            return {"ok": False, "code": "BACKUP_EXPORT_FAILED", "message": str(exc)}

    def import_config_backup(self) -> dict[str, Any]:
        if self._preview_state:
            return {"ok": True, "restored_plugin_configs": 2, "config": self.cfg.to_dict()}
        if self._window is None:
            return {"ok": False, "code": "NO_WINDOW"}
        try:
            import webview

            picked = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("WireDeck Backup (*.json)", "All files (*.*)"),
            )
            if not picked:
                return {"ok": False, "code": "CANCELLED"}
            chosen = str(picked[0] if isinstance(picked, (list, tuple)) else picked).strip()
            if not chosen:
                return {"ok": False, "code": "CANCELLED"}
            res = import_config_backup(self.paths, Path(chosen))
            if res.get("ok"):
                self.cfg = load_config(self.paths)
            return res
        except Exception as exc:
            return {"ok": False, "code": "BACKUP_IMPORT_FAILED", "message": str(exc)}

    def recorder_sync_obs_path(self) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下已模拟同步 OBS 输出目录、分段与采集源").to_dict()
        rec = self.cfg.recorder
        tgt = resolve_capture_target(self.cfg, win32_ops=self._win32_ops)
        return sync_obs_output_dir(
            rec.output_dir.strip(),
            paths=self.paths,
            split_mode=rec.split_mode,
            split_minutes=rec.split_minutes,
            split_size_mb=rec.split_size_mb,
            capture_target=tgt,
        ).to_dict()

    def recorder_offload_now(self) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下已模拟校验归档片段").to_dict()
        rec = self.cfg.recorder
        target = rec.output_dir.strip()
        if not target:
            return ActionResult(ok=False, code="NO_OUTPUT_DIR", message="请先在「存储与归档」中设置目标录像文件夹", exit_code=1).to_dict()
        src_dirs = (
            [Path(rec.source_watch_dir.strip())]
            if rec.source_watch_dir.strip()
            else [Path.home() / "Videos" / "Captures", Path.home() / "Videos" / "NVIDIA"]
        )
        det = detect_recorders(self.cfg)
        return offload_clips_to_target(
            src_dirs,
            Path(target),
            delete_source=True,
            apply_naming=rec.apply_naming_on_offload,
            filename_template=rec.filename_template,
            preset=self.cfg.active_preset,
            backend=str(det.get("effective_backend") or "obs"),
            mode=rec.mode,
        ).to_dict()

    def recorder_migrate_fallback(self) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下已模拟迁回备用盘暂存片段").to_dict()
        rec = self.cfg.recorder
        target = rec.output_dir.strip()
        fb = rec.fallback_dir.strip()
        if not target or not fb:
            return ActionResult(ok=False, code="NO_FALLBACK_DIR", message="请先同时设置主目标目录与本地备用目录", exit_code=1).to_dict()
        t_st = inspect_target_path(target, bitrate_mbps=rec.ffmpeg_bitrate_mbps, min_free_gb=rec.min_free_gb)
        if not t_st.get("online"):
            return ActionResult(ok=False, code="TARGET_OFFLINE", message="目标外接盘当前未在线，请先插好 U盘/移动硬盘", exit_code=1).to_dict()
        det = detect_recorders(self.cfg)
        return offload_clips_to_target(
            [Path(fb)],
            Path(target),
            delete_source=True,
            apply_naming=rec.apply_naming_on_offload,
            filename_template=rec.filename_template,
            preset=self.cfg.active_preset,
            backend=str(det.get("effective_backend") or "obs"),
            mode=rec.mode,
        ).to_dict()

    def recorder_rename_clip(self, clip_path: str, new_body: str) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message=f"预览模式下模拟已重命名为 1_{new_body}").to_dict()
        target = self.cfg.recorder.output_dir.strip()
        if not target:
            return ActionResult(ok=False, code="NO_OUTPUT_DIR", message="未设置目标目录", exit_code=1).to_dict()
        return rename_clip_with_forced_seq(Path(target), Path(clip_path), new_body).to_dict()

    def recorder_prune_now(self) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下已模拟按配额清理").to_dict()
        target = self.cfg.recorder.output_dir.strip()
        if not target:
            return ActionResult(ok=False, code="NO_OUTPUT_DIR", message="请先设置目标录像文件夹", exit_code=1).to_dict()
        return prune_clips_by_quota(
            Path(target),
            max_storage_gb=self.cfg.recorder.max_storage_gb,
            min_free_gb=self.cfg.recorder.min_free_gb,
        ).to_dict()

    def recorder_open_clip(self, clip_path: str) -> dict[str, Any]:
        if self._preview_state:
            return {"ok": True}
        try:
            p = Path(clip_path)
            if p.is_file():
                os.startfile(str(p))  # type: ignore[attr-defined]
                return {"ok": True}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": False, "message": "文件不存在"}

    def recorder_reveal_clip(self, clip_path: str) -> dict[str, Any]:
        if self._preview_state:
            return {"ok": True}
        try:
            p = Path(clip_path)
            if p.exists():
                subprocess.Popen(["explorer.exe", "/select,", str(p)])
                return {"ok": True}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": False, "message": "文件不存在"}

    def recorder_delete_clip(self, clip_path: str) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(ok=True, code="PREVIEW_ONLY", message="预览模式下已模拟删除片段").to_dict()
        target = self.cfg.recorder.output_dir.strip()
        if not target:
            return ActionResult(ok=False, code="NO_OUTPUT_DIR", message="未设置目标目录", exit_code=1).to_dict()
        return delete_clip_file(Path(target), Path(clip_path)).to_dict()

    def pick_recorder_folder(self) -> dict[str, Any]:
        if self._preview_state:
            return {"ok": True, "path": "E:\\XboxClips"}
        if self._window is None:
            return {"ok": False, "code": "NO_WINDOW"}
        try:
            import webview

            picked = self._window.create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
            if not picked:
                return {"ok": False, "code": "CANCELLED"}
            chosen = str(picked[0] if isinstance(picked, (list, tuple)) else picked).strip()
            return {"ok": True, "path": chosen} if chosen else {"ok": False, "code": "CANCELLED"}
        except Exception as exc:
            return {"ok": False, "code": "PICK_FAILED", "message": str(exc)}

    def pick_recorder_exe(self, kind: str = "custom") -> dict[str, Any]:
        return self.pick_plugin_file("Executable (*.exe)")

    def open_folder(self, kind: str) -> dict[str, Any]:
        mapping = {
            "extensions": self.paths.extensions_dir,
            "plugins": self.paths.plugins_dir,
            "logs": self.paths.logs_dir,
            "exports": self.paths.exports_dir,
            "data": self.paths.data_dir,
        }
        if kind == "recorder" and self.cfg.recorder.output_dir.strip():
            target = Path(self.cfg.recorder.output_dir.strip())
        else:
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

    def check_updates(self, force_channel: str = "", force_url: str = "") -> dict[str, Any]:
        if self._preview_state == "update":
            return {
                "ok": True,
                "checked_at": "2026-11-28T01:00:00Z",
                "total_updates": 2,
                "has_any_update": True,
                "app": {
                    "ok": True,
                    "has_update": True,
                    "current_version": APP_VERSION,
                    "latest_version": "0.3.0",
                    "channel": force_channel or self.cfg.update_channel,
                    "feed_url": self.cfg.update_feed_url or "https://api.github.com/repos/Iraryi/WireDeck/releases/latest",
                    "is_portable": self.paths.is_portable,
                    "asset_name": "WireDeck-v0.3.0-win64-setup.exe",
                    "download_url": "https://github.com/Iraryi/WireDeck/releases/download/v0.3.0/WireDeck-v0.3.0-win64-setup.exe",
                    "sha256": "a" * 64,
                    "sha256_ready": True,
                    "size_bytes": 17_820_416,
                    "release_notes": "1. 优化 2.4G/5G 双频热切延迟表现\n2. 补强长期增量更新与自定义镜像源支持\n3. 增强多录屏源自动切片归档稳定性",
                    "release_url": "https://github.com/Iraryi/WireDeck/releases/tag/v0.3.0",
                    "error": "",
                },
                "plugins": {
                    "ok": True,
                    "installed_count": 2,
                    "updates_count": 1,
                    "up_to_date_count": 1,
                    "updates_available": [
                        {
                            "id": "framegen-sharpen",
                            "name": "补帧与超分增强 (Lossless / Magpie)",
                            "current_version": "1.0.0",
                            "latest_version": "1.1.0",
                            "has_update": True,
                        }
                    ],
                    "plugins": [
                        {
                            "id": "framegen-sharpen",
                            "name": "补帧与超分增强 (Lossless / Magpie)",
                            "enabled": True,
                            "current_version": "1.0.0",
                            "latest_version": "1.1.0",
                            "has_update": True,
                            "in_catalog": True,
                        },
                        {
                            "id": "nvidia-driver-toolkit",
                            "name": "NVIDIA 显卡优化工具包",
                            "enabled": True,
                            "current_version": "1.0.0",
                            "latest_version": "1.0.0",
                            "has_update": False,
                            "in_catalog": True,
                        },
                    ],
                    "error": "",
                },
            }
        res = check_all_updates(
            self.paths,
            self.cfg,
            override_channel=force_channel,
            override_url=force_url,
        )
        self._last_update_check = res
        return res

    def save_update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        if "update_channel" in patch:
            ch = str(patch["update_channel"] or "stable").strip().lower()
            if ch in ("stable", "beta"):
                self.cfg.update_channel = ch
        if "update_feed_url" in patch:
            self.cfg.update_feed_url = str(patch["update_feed_url"] or "").strip()
        if "auto_check_updates" in patch:
            self.cfg.auto_check_updates = bool(patch["auto_check_updates"])
        if not self._preview_state:
            save_config(self.paths, self.cfg)
        return {"ok": True, "config": self.cfg.to_dict()}

    def apply_core_update(self, update_info: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(
                ok=True,
                code="PREVIEW_ONLY",
                message="预览模式下已模拟校验 SHA-256 并启动覆盖更新",
                details={"mode": "installer", "should_exit": False},
            ).to_dict()
        info = update_info or (self._last_update_check or {}).get("app")
        if not isinstance(info, dict) or not info.get("download_url"):
            chk = check_all_updates(self.paths, self.cfg)
            self._last_update_check = chk
            info = chk.get("app") or {}
        if not info.get("has_update"):
            return ActionResult(ok=True, code="UP_TO_DATE", message=f"当前 v{APP_VERSION} 已是最新版本").to_dict()
        stg = download_and_stage_app_update(info, self.paths)
        if not stg.ok:
            return stg.to_dict()
        ap = apply_staged_app_update(Path(str(stg.details.get("staged_path") or "")), self.paths)
        if ap.ok and ap.details.get("should_exit") and self._window is not None:
            def _close_soon() -> None:
                time.sleep(0.45)
                try:
                    if self._window is not None:
                        self._window.destroy()
                except Exception:
                    pass

            threading.Thread(target=_close_soon, daemon=True).start()
        return ap.to_dict()

    def update_all_plugins(self, plugin_ids: list[str] | None = None) -> dict[str, Any]:
        if self._preview_state:
            return ActionResult(
                ok=True,
                code="PREVIEW_ONLY",
                message="预览模式下已模拟更新全部插件（保留个人参数）",
                details={"updated": [{"id": "framegen-sharpen", "version": "1.1.0"}], "failed": []},
            ).to_dict()
        return updater_update_all_plugins(self.paths, self.cfg, plugin_ids=plugin_ids).to_dict()

    def open_release_url(self, url: str = "") -> dict[str, Any]:
        target = (url or "").strip() or "https://github.com/Iraryi/WireDeck/releases"
        if not target.startswith(("https://", "http://")):
            return {"ok": False, "message": "无效的发布页面链接"}
        webbrowser.open(target)
        return {"ok": True}

    def scan_uninstall_items(self) -> dict[str, Any]:
        if self._preview_state == "uninstall":
            return {
                "ok": True,
                "is_portable": False,
                "exe_dir": "C:\\Users\\Demo\\AppData\\Local\\Programs\\WireDeck",
                "data_dir": "C:\\Users\\Demo\\AppData\\Local\\WireDeck",
                "uninstaller_exe": "C:\\Users\\Demo\\AppData\\Local\\Programs\\WireDeck\\unins000.exe",
                "network_restore": {"needed": True, "nat_active": True, "wifi_tuned": False, "default_selected": False},
                "wiredeck_items": [
                    {"id": "wd_shortcuts", "count": 2, "size_mb": 0.01, "paths": ["Desktop\\XStreaming.lnk"], "default_selected": False},
                    {"id": "wd_plugins", "count": 14, "size_mb": 3.2, "paths": ["data\\plugins"], "default_selected": False},
                    {"id": "wd_userdata", "count": 6, "size_mb": 1.85, "paths": ["LocalAppData\\WireDeck"], "default_selected": False},
                ],
                "associated_apps": [
                    {
                        "id": "assoc_xstreaming",
                        "spec_id": "xstreaming",
                        "name": "XStreaming",
                        "category": "stream_client",
                        "version": "1.9.6",
                        "install_location": "C:\\Program Files\\XStreaming",
                        "uninstall_command": "C:\\Program Files\\XStreaming\\Uninstall.exe",
                        "used_by_wiredeck": True,
                        "default_selected": False,
                    },
                    {
                        "id": "assoc_obs",
                        "spec_id": "obs",
                        "name": "OBS Studio",
                        "category": "recorder",
                        "version": "31.0.1",
                        "install_location": "C:\\Program Files\\obs-studio",
                        "uninstall_command": "C:\\Program Files\\obs-studio\\uninstall.exe",
                        "used_by_wiredeck": True,
                        "default_selected": False,
                    },
                    {
                        "id": "assoc_moonlight",
                        "spec_id": "moonlight",
                        "name": "Moonlight Game Streaming",
                        "category": "stream_client",
                        "version": "6.1.0",
                        "install_location": "C:\\Program Files\\Moonlight Game Streaming",
                        "uninstall_command": "C:\\Program Files\\Moonlight Game Streaming\\uninstall.exe",
                        "used_by_wiredeck": False,
                        "default_selected": False,
                    },
                ],
                "associated_cleanup_default_enabled": False,
                "recorded_clips_protected": True,
                "recorded_clips_dir": "E:\\XboxClips",
            }
        scan = discover_uninstall_items(self.paths, self.cfg)
        self._last_uninstall_scan = scan
        return scan

    def execute_uninstall(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or {}
        selected_ids = [str(x) for x in (data.get("selected_ids") or []) if str(x).strip()]
        restore_network = bool(data.get("restore_network", False))
        if self._preview_state:
            return ActionResult(
                ok=True,
                code="PREVIEW_ONLY",
                message=f"预览模式：已模拟执行 {len(selected_ids)} 项可选清理并准备卸载主程序",
                details={"executed": [{"id": x, "ok": True, "message": "preview"} for x in selected_ids], "failed": []},
            ).to_dict()

        def _restore_net_cb() -> ActionResult:
            ok_r, view_r = _run_quick_restore(self, silent=False)
            return ActionResult(
                ok=ok_r,
                code="NET_RESTORED" if ok_r else "NET_RESTORE_FAILED",
                message="已将 WinNAT / Wi-Fi 恢复为日常网络模式" if ok_r else f"部分网络未能自动恢复（请检查 {view_r}），可稍后手动恢复",
                exit_code=0 if ok_r else 1,
            )

        res = execute_uninstall_cleanup(
            self.paths,
            self.cfg,
            selected_ids,
            restore_network=restore_network,
            discovered=self._last_uninstall_scan,
            restore_network_fn=_restore_net_cb,
        )

        if self._from_uninstaller:
            self._uninstall_exit_code = 0
            def _close_for_inno() -> None:
                time.sleep(0.35)
                try:
                    if self._window is not None:
                        self._window.destroy()
                except Exception:
                    pass

            threading.Thread(target=_close_for_inno, daemon=True).start()
            return res.to_dict()

        self_res = launch_self_uninstall(self.paths)
        combined = res.to_dict()
        combined["self_uninstall"] = self_res.to_dict()
        if self_res.ok and (self_res.details or {}).get("should_exit"):
            def _close_self() -> None:
                time.sleep(0.4)
                try:
                    if self._window is not None:
                        self._window.destroy()
                except Exception:
                    pass

            threading.Thread(target=_close_self, daemon=True).start()
        return combined

    def cancel_uninstall_wizard(self) -> dict[str, Any]:
        self._uninstall_exit_code = 2
        if self._window is not None and (self._uninstall_wizard or self._preview_state == "uninstall"):
            def _close_cancel() -> None:
                time.sleep(0.15)
                try:
                    if self._window is not None:
                        self._window.destroy()
                except Exception:
                    pass

            threading.Thread(target=_close_cancel, daemon=True).start()
        return {"ok": True, "cancelled": True}

    def shutdown(self) -> None:
        if not self._preview_state:
            try:
                if self._recorder.is_recording:
                    self._recorder.stop_recording(self.cfg, self.paths, win32_ops=self._win32_ops)
            except Exception:
                pass
            try:
                emit_plugin_hook(self.paths, "on_app_exit", _build_hook_context(self.cfg), win32_ops=self._win32_ops)
            except Exception:
                pass
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


def _build_hook_context(
    cfg: AppConfig,
    *,
    silent: bool = False,
    paths: AppPaths | None = None,
    include_system_env: bool = True,
    plugin_overrides: dict[str, dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    peer_ip = cfg.wireless_peer_ip if cfg.active_transport == "wireless" else cfg.peer_ip
    preset = get_preset(cfg.active_preset)
    client_info = resolve_stream_client_info(preset.id, cfg.custom_client_path) if preset.uses_stream_client else {}
    client_exe_path = str(client_info.get("exe_path") or "")
    ctx: dict[str, Any] = {
        "preset": cfg.active_preset,
        "transport": cfg.active_transport,
        "submode": cfg.wireless_submode,
        "peer_ip": peer_ip,
        "host_ip": cfg.host_ip,
        "language": cfg.language,
        "silent": bool(silent),
        "client_pid": 0,
        "client_exe": Path(client_exe_path).name if client_exe_path else "",
        "client_exe_path": client_exe_path,
        "client_name": str(client_info.get("name") or preset.official_client_name or preset.id),
        "data_dir": str(paths.data_dir) if paths is not None else "",
        "recorder": cfg.recorder.to_dict(),
        "plugin_overrides": dict(plugin_overrides or {}),
    }
    if include_system_env:
        try:
            ctx["system_env"] = collect_system_env_cached()
        except Exception:
            ctx["system_env"] = {}
    if extra:
        ctx.update(extra)
    return ctx


def _parse_plugin_opt_args(raw_opts: list[str] | None) -> dict[str, dict[str, Any]]:
    """Parse repeatable CLI `--plugin-opt plugin_id.key=value` flags cleanly."""
    overrides: dict[str, dict[str, Any]] = {}
    for item in raw_opts or []:
        if "=" not in item or "." not in item.split("=", 1)[0]:
            continue
        lhs, rhs = item.split("=", 1)
        pid, key = lhs.split(".", 1)
        pid, key = pid.strip(), key.strip()
        if not pid or not key:
            continue
        low = rhs.strip().lower()
        val: Any
        if low == "true":
            val = True
        elif low == "false":
            val = False
        else:
            try:
                val = int(rhs) if rhs.strip().isdigit() else float(rhs)
            except ValueError:
                val = rhs
        overrides.setdefault(pid, {})[key] = val
    return overrides


def _log_quick_play_error(paths: AppPaths, tag: str, message: str) -> None:
    try:
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] [{tag}] {message}\n"
        with (paths.logs_dir / "quick_play.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _run_quick_play_detailed(
    api: WireDeckApi,
    *,
    preset_id: str | None = None,
    transport: str | None = None,
    submode: str | None = None,
    launch_fn: Callable[..., ActionResult] = launch_stream_client,
    silent: bool = False,
    skip_plugins: bool = False,
    plugin_overrides: dict[str, dict[str, Any]] | None = None,
    win32_ops: Win32Ops | None = None,
) -> tuple[bool, str, str, dict[str, Any]]:
    """Activate the target transport if idle, run plugin hooks, and launch the stream client."""
    ops = win32_ops or api._win32_ops
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

    ctx = _build_hook_context(
        api.cfg,
        silent=silent,
        paths=api.paths,
        plugin_overrides=plugin_overrides,
        extra={"_recorder_callback": api._make_recorder_callback()},
    )
    if not skip_plugins:
        emit_plugin_hook(api.paths, "before_quick_play", ctx, win32_ops=ops)

    if active_t == "wireless":
        w_snap = api._wireless_snapshot_fn(api.cfg, probe_network=False)
        w_rep = classify_wireless_snapshot(w_snap, api.cfg)
        if active_sub == "hotspot":
            w3 = next((l for l in w_rep.layers if l.layer == "W3"), None)
            hs_on = bool(w3 and w3.metrics.get("hotspot_state") == "On")
            if not hs_on:
                if not skip_plugins:
                    emit_plugin_hook(api.paths, "before_transport_enable", ctx, win32_ops=ops)
                res = api._execute_or_elevate("enable-hotspot", {"force": False})
                if not res.ok:
                    return False, fallback_view, res.message, {}
                if not skip_plugins:
                    emit_plugin_hook(api.paths, "after_transport_enable", ctx, win32_ops=ops)
        else:
            w1 = next((l for l in w_rep.layers if l.layer == "W1"), None)
            w2 = next((l for l in w_rep.layers if l.layer == "W2"), None)
            w4 = next((l for l in w_rep.layers if l.layer == "W4"), None)
            if w2 and w2.status == Severity.FAIL:
                return False, fallback_view, w2.summary or "Wi-Fi 尚未连接到路由器，请先连接 Wi-Fi", {}
            anti_on = bool(w4 and w4.metrics.get("anti_stutter_active"))
            if not anti_on:
                if w1 and w1.status == Severity.FAIL:
                    return False, fallback_view, w1.summary or "未检测到可用无线网卡", {}
                if not skip_plugins:
                    emit_plugin_hook(api.paths, "before_transport_enable", ctx, win32_ops=ops)
                res = api._execute_or_elevate("optimize-wifi", {})
                if not res.ok:
                    return False, fallback_view, res.message, {}
                if not skip_plugins:
                    emit_plugin_hook(api.paths, "after_transport_enable", ctx, win32_ops=ops)
    else:
        snap = api._snapshot_fn(api.cfg, probe_network=False)
        rep = classify_snapshot(snap, api.cfg)
        l4 = next((l for l in rep.layers if l.layer == "L4"), None)
        nat_on = bool(l4 and l4.metrics.get("nat_active"))
        if not nat_on:
            if not rep.can_enable_direct:
                msg = rep.primary_fault.message if rep.primary_fault else (rep.headline or "有线直连前置条件未就绪")
                return False, fallback_view, msg, {}
            if not skip_plugins:
                emit_plugin_hook(api.paths, "before_transport_enable", ctx, win32_ops=ops)
            res = api._execute_or_elevate("enable-direct", {})
            if not res.ok:
                return False, fallback_view, res.message, {}
            if not skip_plugins:
                emit_plugin_hook(api.paths, "after_transport_enable", ctx, win32_ops=ops)

    preset = get_preset(api.cfg.active_preset)
    client_info = resolve_stream_client_info(preset.id, api.cfg.custom_client_path) if preset.uses_stream_client else {}
    ctx["client_name"] = str(client_info.get("name") or preset.official_client_name or preset.id)
    if client_info.get("exe_path"):
        ctx["client_exe_path"] = str(client_info["exe_path"])
        ctx["client_exe"] = Path(str(client_info["exe_path"])).name

    if not skip_plugins:
        emit_plugin_hook(api.paths, "before_client_launch", ctx, win32_ops=ops)

    if preset.uses_stream_client:
        custom = api.cfg.custom_client_path.strip()
        if launch_fn is launch_stream_client:
            launch_res = launch_fn(preset_id=preset.id, custom_path=custom)
        else:
            launch_res = launch_fn(preset_id=preset.id)
        if not launch_res.ok:
            return False, fallback_view, launch_res.message or "未检测到可用的串流客户端", dict(launch_res.details)
        launch_details = dict(launch_res.details or {})
        ctx["client_pid"] = int(launch_details.get("pid") or 0)
        if launch_details.get("path"):
            ctx["client_exe_path"] = str(launch_details["path"])
        if launch_details.get("exe_name"):
            ctx["client_exe"] = str(launch_details["exe_name"])
        if launch_details.get("client_name"):
            ctx["client_name"] = str(launch_details["client_name"])
        if not skip_plugins:
            emit_plugin_hook(api.paths, "after_client_launch", ctx, win32_ops=ops)
            emit_plugin_hook(api.paths, "after_quick_play", ctx, win32_ops=ops)
        return True, "", "", launch_details

    if not skip_plugins:
        emit_plugin_hook(api.paths, "after_quick_play", ctx, win32_ops=ops)
    return True, "", "", {"client_name": preset.official_client_name or preset.id, "pid": 0, "exe_name": ""}


def _run_quick_play(
    api: WireDeckApi,
    *,
    preset_id: str | None = None,
    transport: str | None = None,
    submode: str | None = None,
    launch_fn: Callable[..., ActionResult] = launch_stream_client,
) -> tuple[bool, str]:
    """Activate the target transport if idle, then launch the preset stream client."""
    ok, fallback_view, _, _ = _run_quick_play_detailed(
        api,
        preset_id=preset_id,
        transport=transport,
        submode=submode,
        launch_fn=launch_fn,
        silent=False,
        win32_ops=api._win32_ops,
    )
    return ok, fallback_view


def _run_quick_restore(api: WireDeckApi, *, silent: bool = False) -> tuple[bool, str]:
    """Restore active wired WinNAT, 5G hotspot, and Wi-Fi anti-stutter tuning back to daily normal."""
    ctx = _build_hook_context(api.cfg, silent=silent)
    emit_plugin_hook(api.paths, "before_quick_restore", ctx, win32_ops=api._win32_ops)

    snap = api._snapshot_fn(api.cfg, probe_network=False)
    rep = classify_snapshot(snap, api.cfg)
    l4 = next((l for l in rep.layers if l.layer == "L4"), None)
    if l4 and l4.metrics.get("nat_active"):
        emit_plugin_hook(api.paths, "before_transport_disable", ctx, win32_ops=api._win32_ops)
        res = api._execute_or_elevate("disable-direct", {})
        if not res.ok:
            return False, "home"
        emit_plugin_hook(api.paths, "after_transport_disable", ctx, win32_ops=api._win32_ops)

    w_snap = api._wireless_snapshot_fn(api.cfg, probe_network=False)
    hs_on = str((w_snap.get("hotspot") or {}).get("state", "")).lower() == "on"
    if hs_on:
        emit_plugin_hook(api.paths, "before_transport_disable", ctx, win32_ops=api._win32_ops)
        res = api._execute_or_elevate("disable-hotspot", {})
        if not res.ok:
            return False, "wireless"
        emit_plugin_hook(api.paths, "after_transport_disable", ctx, win32_ops=api._win32_ops)

    if (api.paths.runtime_dir / "wifi_backup.json").is_file():
        res = api._execute_or_elevate("restore-wifi", {})
        if not res.ok:
            return False, "wireless"

    emit_plugin_hook(api.paths, "after_quick_restore", ctx, win32_ops=api._win32_ops)
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="WireDeck", add_help=True)
    parser.add_argument(
        "--preview-state",
        choices=["ready", "breakpoint", "wizard", "ai", "settings", "onboarding", "wireless", "wireless_lan", "wireless_wizard", "plugins", "recorder", "update", "uninstall"],
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
        "--silent",
        action="store_true",
        help="Never pop up WireDeck GUI window on quick-play/restore; use silent tray notification on error",
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
    parser.add_argument(
        "--no-plugins",
        action="store_true",
        help="Skip plugin hooks during --quick-play (safe-mode launch)",
    )
    parser.add_argument(
        "--plugin-opt",
        action="append",
        default=[],
        help="Temporary plugin override in plugin_id.key=value format",
    )
    parser.add_argument(
        "--uninstall-wizard",
        action="store_true",
        help="Open dedicated interactive WireDeck uninstall wizard",
    )
    parser.add_argument(
        "--from-uninstaller",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    is_uninstall_mode = bool(args.uninstall_wizard or args.preview_state == "uninstall")
    api = WireDeckApi(
        preview_state=args.preview_state,
        preview_wizard_step=args.wizard_step,
        uninstall_wizard=bool(args.uninstall_wizard),
        from_uninstaller=bool(args.from_uninstaller),
    )
    if args.theme:
        api.cfg.theme = args.theme
    if args.lang:
        api.cfg.language = args.lang
    title = (
        f"{APP_NAME} Uninstall"
        if is_uninstall_mode and api.cfg.language == "en"
        else (f"{APP_NAME} 卸载向导" if is_uninstall_mode else f"{APP_NAME} v{APP_VERSION}")
    )
    plugin_overrides = _parse_plugin_opt_args(args.plugin_opt)

    if args.quick_play and not args.preview_state:
        done, _, err_msg, launch_details = _run_quick_play_detailed(
            api,
            preset_id=args.preset,
            transport=args.transport,
            submode=args.submode,
            silent=bool(args.silent),
            skip_plugins=bool(args.no_plugins),
            plugin_overrides=plugin_overrides,
            win32_ops=api._win32_ops,
        )
        if done:
            client_pid = int(launch_details.get("pid") or 0)
            if args.silent:
                try:
                    health = api.get_system_health()
                    for w in health.get("warnings") or []:
                        if w.get("code") in ("ON_BATTERY", "REC_TARGET_OFFLINE"):
                            api._win32_ops.show_notification("WireDeck", f"{w.get('title')}：{w.get('detail')}")
                            break
                except Exception:
                    pass

                def _graceful_sig(_signum: int, _frame: Any) -> None:
                    try:
                        if api._recorder.is_recording:
                            api._recorder.stop_recording(api.cfg, api.paths, win32_ops=api._win32_ops)
                    except Exception:
                        pass
                    if api.cfg.keep_awake_during_stream:
                        try:
                            api._win32_ops.set_keep_awake(False)
                        except Exception:
                            pass

                for sig_name in ("SIGINT", "SIGTERM"):
                    sig = getattr(signal, sig_name, None)
                    if sig is not None:
                        try:
                            signal.signal(sig, _graceful_sig)
                        except Exception:
                            pass

                api._start_session_supervisor(
                    client_pid,
                    launch_details,
                    skip_plugins=bool(args.no_plugins),
                    plugin_overrides=plugin_overrides,
                    blocking=True,
                )
            else:
                api._start_session_supervisor(
                    client_pid,
                    launch_details,
                    skip_plugins=bool(args.no_plugins),
                    plugin_overrides=plugin_overrides,
                    blocking=False,
                )
            return 0
        if args.silent:
            reason = err_msg or "配置前置条件未就绪或未找到串流客户端，请打开 WireDeck 检查"
            _log_quick_play_error(api.paths, "quick-play", reason)
            api._win32_ops.show_notification("WireDeck", reason)
            return 1
    elif args.quick_restore and not args.preview_state:
        done, _ = _run_quick_restore(api, silent=bool(args.silent))
        if done:
            return 0
        if args.silent:
            reason = "一键恢复日常网络未完全成功，请打开 WireDeck 查看状态"
            _log_quick_play_error(api.paths, "quick-restore", reason)
            api._win32_ops.show_notification("WireDeck", reason)
            return 1

    mutex_handle: int | None = None
    if not args.preview_state and not args.capture_to and not args.uninstall_wizard:
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

    win_w, win_h = (660, 565) if is_uninstall_mode else (840, 520)
    min_sz = (600, 480) if is_uninstall_mode else (760, 480)
    window = webview.create_window(
        title=title,
        url=index_url,
        js_api=api,
        width=win_w,
        height=win_h,
        min_size=min_sz,
        background_color=bg_hex,
        resizable=True,
    )
    api._window = window

    def _on_started() -> None:
        _set_titlebar_theme(title, api.cfg.theme == "dark")
        if not args.preview_state and not args.capture_to and not args.uninstall_wizard:
            threading.Thread(
                target=lambda: emit_plugin_hook(
                    api.paths,
                    "on_app_start",
                    _build_hook_context(api.cfg),
                    win32_ops=api._win32_ops,
                ),
                daemon=True,
            ).start()
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
    return int(api._uninstall_exit_code) if args.uninstall_wizard else 0


if __name__ == "__main__":
    raise SystemExit(main())
