"""Optional (OFF by default) localhost HTTP JSON Bridge for DSH / AI integration."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from wiredeck.actions import SUPPORTED_ACTIONS
from wiredeck.config import load_config
from wiredeck.extensions import discover_extensions
from wiredeck.models import ActionResult, DiagnosticReport
from wiredeck.paths import AppPaths
from wiredeck.toolkits import discover_toolkits, run_toolkit
from wiredeck.version import APP_VERSION


@dataclass(frozen=True)
class BridgeInfo:
    host: str
    port: int
    url: str
    token: str
    started_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BridgeJob:
    job_id: str
    action: str
    opts: dict[str, Any]
    status: str  # pending_approval | running | completed | rejected | failed
    created_at: str
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LocalBridgeServer:
    """Ephemeral 127.0.0.1 HTTP JSON server with Bearer auth and human-approved write jobs."""

    def __init__(
        self,
        *,
        paths: AppPaths,
        diagnose_fn: Callable[[], DiagnosticReport],
        execute_action_fn: Callable[[str, dict[str, Any]], ActionResult],
        on_job_queued: Callable[[BridgeJob], None] | None = None,
        diagnose_wireless_fn: Callable[[], DiagnosticReport] | None = None,
    ) -> None:
        self._paths = paths
        self._diagnose_fn = diagnose_fn
        self._diagnose_wireless_fn = diagnose_wireless_fn
        self._execute_action_fn = execute_action_fn
        self._on_job_queued = on_job_queued

        self._lock = threading.Lock()
        self._jobs: dict[str, BridgeJob] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._info: BridgeInfo | None = None

    @property
    def info(self) -> BridgeInfo | None:
        return self._info

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._info is not None

    def _bridge_file(self) -> Path:
        return self._paths.runtime_dir / "bridge.json"

    def start(self) -> BridgeInfo:
        if self._info is not None and self._server is not None:
            return self._info

        token = secrets.token_hex(32)
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _send_json(self, code: int, payload: dict[str, Any]) -> None:
                raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _check_auth(self) -> bool:
                auth = self.headers.get("Authorization", "")
                expected = f"Bearer {token}"
                if not hmac.compare_digest(auth, expected):
                    self._send_json(401, {"ok": False, "code": "UNAUTHORIZED", "message": "Invalid Bearer token"})
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                if not self._check_auth():
                    return
                path, _, qs = self.path.partition("?")
                if path == "/api/v1/health":
                    self._send_json(200, {"ok": True, "version": APP_VERSION})
                    return
                if path == "/api/v1/diagnose":
                    try:
                        want_wireless = "transport=wireless" in qs.lower()
                        fn = (
                            parent._diagnose_wireless_fn
                            if (want_wireless and parent._diagnose_wireless_fn is not None)
                            else parent._diagnose_fn
                        )
                        report = fn()
                        self._send_json(200, report.to_dict())
                    except Exception as exc:
                        self._send_json(500, {"ok": False, "code": "DIAGNOSE_ERROR", "message": str(exc)})
                    return
                if path == "/api/v1/extensions":
                    exts = [m.to_dict() for m in discover_extensions(parent._paths)]
                    self._send_json(200, {"extensions": exts})
                    return
                if path == "/api/v1/toolkits":
                    cfg = load_config(parent._paths)
                    tools = [t.to_dict() for t in discover_toolkits(preset_id=cfg.active_preset, include_common=True)]
                    self._send_json(200, {"active_preset": cfg.active_preset, "toolkits": tools})
                    return
                if path.startswith("/api/v1/jobs/"):
                    job_id = path[len("/api/v1/jobs/") :].strip("/")
                    with parent._lock:
                        job = parent._jobs.get(job_id)
                    if job is None:
                        self._send_json(404, {"ok": False, "code": "JOB_NOT_FOUND"})
                        return
                    self._send_json(200, job.to_dict())
                    return
                self._send_json(404, {"ok": False, "code": "NOT_FOUND"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._check_auth():
                    return
                path = self.path.split("?", 1)[0]
                if path == "/api/v1/toolkits/run":
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                        body = self.rfile.read(min(length, 16384)).decode("utf-8") if length > 0 else "{}"
                        payload = json.loads(body) if body.strip() else {}
                    except Exception:
                        self._send_json(400, {"ok": False, "code": "BAD_JSON"})
                        return
                    tid = str(payload.get("tool_id") or "").strip()
                    cfg = load_config(parent._paths)
                    res = run_toolkit(tid, cfg=cfg)
                    self._send_json(200 if res.ok else 400, res.to_dict())
                    return
                if path != "/api/v1/apply":
                    self._send_json(404, {"ok": False, "code": "NOT_FOUND"})
                    return

                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(min(length, 65536)).decode("utf-8") if length > 0 else "{}"
                    payload = json.loads(body) if body.strip() else {}
                except Exception:
                    self._send_json(400, {"ok": False, "code": "BAD_JSON", "message": "Invalid JSON body"})
                    return

                action = str(payload.get("action") or "").strip()
                if action not in SUPPORTED_ACTIONS:
                    self._send_json(
                        400,
                        {"ok": False, "code": "UNKNOWN_ACTION", "message": f"Unsupported action: {action}"},
                    )
                    return

                opts = {k: v for k, v in payload.items() if k != "action"}
                job = BridgeJob(
                    job_id=uuid.uuid4().hex[:12],
                    action=action,
                    opts=opts,
                    status="pending_approval",
                    created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                )
                with parent._lock:
                    parent._jobs[job.job_id] = job
                if parent._on_job_queued is not None:
                    try:
                        parent._on_job_queued(job)
                    except Exception:
                        pass
                self._send_json(202, job.to_dict())

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = int(server.server_address[1])
        info = BridgeInfo(
            host="127.0.0.1",
            port=port,
            url=f"http://127.0.0.1:{port}",
            token=token,
            started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        self._server = server
        self._info = info

        self._paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        bridge_payload = info.to_dict()
        bridge_payload["pid"] = os.getpid()
        self._bridge_file().write_text(
            json.dumps(bridge_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        t = threading.Thread(target=server.serve_forever, name="wiredeck-bridge", daemon=True)
        t.start()
        self._thread = t
        return info

    def list_pending_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.to_dict() for j in self._jobs.values() if j.status == "pending_approval"]

    def approve_job(self, job_id: str) -> BridgeJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "pending_approval":
                return job
            job.status = "running"

        try:
            res = self._execute_action_fn(job.action, job.opts)
            with self._lock:
                job.result = res.to_dict()
                job.status = "completed" if res.ok else "failed"
        except Exception as exc:
            with self._lock:
                job.result = ActionResult(ok=False, code="BRIDGE_EXEC_ERROR", message=str(exc), exit_code=1).to_dict()
                job.status = "failed"
        return job

    def reject_job(self, job_id: str) -> BridgeJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "pending_approval":
                return job
            job.status = "rejected"
            job.result = ActionResult(
                ok=False, code="USER_REJECTED", message="用户已在界面拒绝此操作", exit_code=3
            ).to_dict()
            return job

    def stop(self) -> None:
        server = self._server
        self._server = None
        self._info = None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self._bridge_file().unlink(missing_ok=True)
        except OSError:
            pass
