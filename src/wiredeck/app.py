"""GUI entrypoint (`WireDeck.exe`) using Edge WebView2 via pywebview."""

from __future__ import annotations

import argparse
import ctypes
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
from wiredeck.probes import collect_system_snapshot
from wiredeck.ps_runner import is_admin, run_elevated_worker, run_powershell
from wiredeck.stream_client import (
    download_and_verify_asset,
    fetch_latest_xstreaming_release,
    launch_stream_client,
)
from wiredeck.version import APP_NAME, APP_VERSION, NAT_NAME


def build_preview_report(preview_mode: str) -> DiagnosticReport:
    """Build deterministic, zero-PII diagnostic reports for clean high-DPI screenshots."""
    cfg = AppConfig()
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
            {"interface_alias": "Ethernet", "interface_index": 12, "ip": "192.168.50.2", "prefix_length": 24},
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
        "stream_clients": {"xstreaming_path": "C:\\Program Files\\XStreaming\\XStreaming.exe", "xbox_app_installed": True},
        "connectivity": {"peer_reachable": True, "peer_rtt_ms": 0.4, "host_internet_ok": True, "xbox_live_ok": True},
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


class WireDeckApi:
    """Python API surface exposed to the WebView2 frontend via pywebview."""

    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        snapshot_fn: Callable[..., dict[str, Any]] = collect_system_snapshot,
        preview_state: str | None = None,
    ) -> None:
        self.paths = paths or resolve_app_paths()
        self.cfg = load_config(self.paths)
        self._snapshot_fn = snapshot_fn
        self._preview_state = preview_state
        self._last_report: DiagnosticReport | None = None
        self._bridge: LocalBridgeServer | None = None

    def _preview_view_name(self) -> str | None:
        if self._preview_state == "wizard":
            return "wizard"
        if self._preview_state == "ai":
            return "ai"
        return None

    def get_state(self) -> dict[str, Any]:
        bridge_running = self._bridge is not None and self._bridge.is_running
        bridge_info: dict[str, Any] = {
            "running": bridge_running,
            "url": self._bridge.info.url if (bridge_running and self._bridge.info) else "",
            "token": self._bridge.info.token if (bridge_running and self._bridge.info) else "",
            "pending_jobs": self._bridge.list_pending_jobs() if bridge_running else [],
        }
        return {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "is_admin": is_admin(),
            "config": self.cfg.to_dict(),
            "paths": self.paths.to_dict(),
            "bridge": bridge_info,
            "preview_view": self._preview_view_name(),
        }

    def diagnose(self) -> dict[str, Any]:
        if self._preview_state:
            rep = build_preview_report(self._preview_state)
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

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        merged = self.cfg.to_dict()
        for k in ("language", "console_profile", "ethernet_interface", "uplink_interface", "host_ip", "peer_ip"):
            if k in patch:
                merged[k] = patch[k]
        from wiredeck.config import _normalize_config

        self.cfg = _normalize_config(merged)
        save_config(self.paths, self.cfg)
        return {"ok": True, "config": self.cfg.to_dict()}

    def export_for_ai(self) -> dict[str, Any]:
        report = self._last_report or (
            build_preview_report(self._preview_state)
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

    def toggle_bridge(self, enabled: bool) -> dict[str, Any]:
        if enabled:
            if self._bridge is None:
                self._bridge = LocalBridgeServer(
                    paths=self.paths,
                    diagnose_fn=lambda: run_diagnosis(
                        self.paths, self.cfg, include_extensions=True, probe_network=True, snapshot_fn=self._snapshot_fn
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
        return launch_stream_client().to_dict()

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


def _enable_dark_titlebar(title: str) -> None:
    """Apply Windows 11/10 dark immersive title bar matching WireDeck's obsidian theme."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        for _ in range(20):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                val = ctypes.c_int(1)
                dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
                return
            time.sleep(0.1)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="WireDeck", add_help=True)
    parser.add_argument(
        "--preview-state",
        choices=["ready", "breakpoint", "wizard", "ai"],
        default=None,
        help="Open GUI in deterministic read-only preview mode",
    )
    parser.add_argument(
        "--capture-to",
        default=None,
        help="Optional PNG output path; captures window screenshot and exits cleanly",
    )
    args = parser.parse_args(argv)

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

    api = WireDeckApi(preview_state=args.preview_state)
    web_dir = get_web_dir()
    index_url = (web_dir / "index.html").resolve().as_uri()
    title = f"{APP_NAME} v{APP_VERSION}"

    window = webview.create_window(
        title=title,
        url=index_url,
        js_api=api,
        width=980,
        height=700,
        min_size=(860, 620),
        background_color="#080d18",
        resizable=True,
    )

    def _on_started() -> None:
        _enable_dark_titlebar(title)
        if args.capture_to:
            out_p = Path(args.capture_to)
            _capture_window_screenshot(title, out_p, delay_sec=2.0)
            try:
                window.destroy()
            except Exception:
                pass

    def _on_closed() -> None:
        api.shutdown()

    window.events.closed += _on_closed
    icon_file = get_assets_dir() / "wiredeck.ico"
    start_kwargs: dict[str, Any] = {"gui": "edgechromium", "debug": False}
    if icon_file.is_file():
        start_kwargs["icon"] = str(icon_file)

    webview.start(_on_started, **start_kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
