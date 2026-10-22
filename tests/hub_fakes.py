"""Shared fakes for hub tests: listeners that behave like RelayClient without a network."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from privy.client import ExecResult
from privy.hub.app import Headers, HttpResponse, HubApp
from privy.hub.config import RelaysStore
from privy.hub.jobs import AuditLog, JobManager
from privy.hub.registry import TargetRegistry
from privy.hub.service import HubService
from privy.transfer import TransferResult

TOKEN = "test-hub-token"
PORT = 7748
HOST = f"127.0.0.1:{PORT}"


def exec_result(
    *,
    exit_code: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timed_out: bool = False,
    error: str | None = None,
    job_id: str | None = "remote-1",
) -> ExecResult:
    return ExecResult(
        exit_code=exit_code,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
        stdout_bytes=stdout,
        stderr_bytes=stderr,
        duration_ms=1,
        timed_out=timed_out,
        error=error,
        job_id=job_id,
    )


def echo_run(request, on_output, on_submit, listener):
    on_submit("remote-1")
    data = f"ran {request.kind}: {request.code}\n".encode()
    on_output("stdout", data)
    return exec_result(stdout=data)


def blocking_run(request, on_output, on_submit, listener):
    """Run until the hub cancels the remote job (or the test releases it)."""
    on_submit("remote-blocking")
    on_output("stdout", b"started\n")
    listener.started.set()
    listener.cancel_event.wait(10)
    return exec_result(exit_code=130, stdout=b"started\n", error="cancelled")


class FakeListener:
    """Behaviour of one fake target, shared by every client the hub creates for it."""

    def __init__(
        self,
        *,
        info: dict[str, Any] | None = None,
        legacy: bool = False,
        info_error: BaseException | None = None,
        run: Callable[..., ExecResult] = echo_run,
        transfer: Callable[..., TransferResult] | None = None,
        hostname: str = "box",
    ) -> None:
        if info is None and not legacy:
            info = {
                "kind": "listener_info",
                "hostname": hostname,
                "user": "operator",
                "privy_version": "0.2.0",
                "platform": {"system": "Windows", "description": "Windows-11", "machine": "AMD64"},
                "capabilities": {"languages": ["python", "bash", "powershell"]},
                "jobs": {"running": 0, "retained": 0},
                "uptime_s": 5,
            }
        self.info = info
        self.info_error = info_error
        self.run = run
        self.transfer = transfer
        self.info_calls = 0
        self.requests: list[Any] = []
        self.cancelled: list[str] = []
        self.cancel_event = threading.Event()
        self.started = threading.Event()


class FakeRelayClient:
    def __init__(self, listener: FakeListener) -> None:
        self.listener = listener

    def listener_info(self, *, http_timeout_s: float = 15.0) -> dict[str, Any] | None:
        self.listener.info_calls += 1
        if self.listener.info_error is not None:
            raise self.listener.info_error
        return self.listener.info

    def send(self, request, *, async_job=None, on_output=None, on_submit=None) -> ExecResult:
        self.listener.requests.append(request)
        return self.listener.run(request, on_output, on_submit, self.listener)

    def cancel(self, request, job_id: str) -> ExecResult:
        self.listener.cancelled.append(job_id)
        self.listener.cancel_event.set()
        return exec_result(error=None)

    def upload_file(self, local_path, remote_path, *, chunk_size, overwrite, progress) -> TransferResult:
        assert self.listener.transfer is not None
        return self.listener.transfer("upload", local_path, remote_path, overwrite, progress)

    def download_file(self, remote_path, local_path, *, chunk_size, overwrite, progress) -> TransferResult:
        assert self.listener.transfer is not None
        return self.listener.transfer("download", remote_path, local_path, overwrite, progress)


def relays_document() -> dict[str, Any]:
    return {
        "version": 1,
        "relays": [
            {
                "name": "dev",
                "namespace": "contoso-relay",
                "subscription_id": "00000000-0000-0000-0000-000000000000",
                "hybrid_connections": [
                    {"path": "box1_windows", "keyrule": "all", "key": "a2V5MQ=="},
                    {"path": "box2_windows", "keyrule": "all", "key": "a2V5Mg==", "description": "build box"},
                ],
            },
            {
                "namespace": "contoso-lab.servicebus.windows.net",
                "keyrule": "privy-send",
                "key": "bmFtZXNwYWNl",
                "hybrid_connections": ["gpu1_linux", "box1_windows"],
            },
        ],
    }


def write_relays(path: Path, document: dict[str, Any] | None = None) -> Path:
    path.write_text(json.dumps(relays_document() if document is None else document), encoding="utf-8")
    return path


class Hub:
    """A fully wired in-memory hub with fake listeners."""

    def __init__(self, tmp_path: Path, listeners: dict[str, FakeListener] | None = None, **job_options):
        self.listeners = listeners if listeners is not None else {}
        self.relays_file = write_relays(tmp_path / "relays.json")
        self.store = RelaysStore(self.relays_file, check_interval_s=0)
        self.registry = TargetRegistry(self.store, client_factory=self._client)
        self.audit_path = tmp_path / "audit.jsonl"
        self.jobs = JobManager(self.registry, audit=AuditLog(self.audit_path), **job_options)
        self.service = HubService(registry=self.registry, jobs=self.jobs, version="test")
        self.app = HubApp(self.service, token=TOKEN, port=PORT)

    def _client(self, target) -> FakeRelayClient:
        return FakeRelayClient(self.listeners.setdefault(target.id, FakeListener(hostname=target.path)))

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        token: str | None = TOKEN,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        request_headers = {"Host": HOST}
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        request_headers.update(headers or {})
        raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        return self.app.handle(method, path, Headers(request_headers.items()), raw)
