"""An isolated real-SQL hub with test-owned listeners, also freezable for E2E."""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from pathlib import Path

from privy.client import ExecResult
from privy.hub.app import build_hub
from privy.hub.responses import HttpResponse
from privy.hub.settings import load_settings
from privy.hub.sql_store import AzureSqlConnections, StorageError


class TestConnections:
    def __init__(self, settings):
        self.pool = AzureSqlConnections(settings)
        self.unavailable = threading.Event()

    def __call__(self):
        if self.unavailable.is_set():
            raise StorageError("isolated test connection outage")
        for attempt in range(6):
            try:
                return self.pool()
            except StorageError as exc:
                if attempt == 5 or "40615" not in str(exc.__cause__):
                    raise
                time.sleep(0.5)
        raise AssertionError("bounded connection attempts exhausted")

    def close(self):
        self.pool.close()


class SimulatedListener:
    def __init__(self) -> None:
        self._cancel = threading.Event()

    def listener_info(self, **kwargs):
        return {
            "kind": "listener_info",
            "hostname": "sql-test-simulator",
            "platform": {"system": "Windows"},
            "privy_version": "test",
            "capabilities": {"languages": ["python"], "terminal": {"shells": []}},
        }

    def send(self, request, *, on_output=None, on_submit=None, **kwargs):
        self._cancel.clear()
        if on_submit:
            on_submit("test-remote-job")
        data = f"simulated: {request.code}\n".encode()
        if on_output:
            on_output("stdout", data)
        cancelled = request.code == "block" and self._cancel.wait(60)
        return ExecResult(
            exit_code=130 if cancelled else 0,
            stdout=data.decode(),
            stderr="",
            stdout_bytes=data,
            stderr_bytes=b"",
            duration_ms=1,
            timed_out=False,
            error="cancelled" if cancelled else None,
        )

    def cancel(self, request, remote_id):
        self._cancel.set()
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.port == 7748:
        parser.error("the live hub port is forbidden in this harness")
    args.home.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "relays": [
            {
                "name": "sqltest",
                "namespace": "simulated.invalid",
                "keyrule": "test-only",
                "key": "not-a-real-credential",
                "hybrid_connections": [f"testbox{i:02d}_windows" for i in range(20)],
            }
        ],
    }
    (args.home / "relays.json").write_text(json.dumps(document), encoding="utf-8")
    listeners = {}
    logging.basicConfig(level=logging.WARNING)
    settings = load_settings(args.home, args.settings)
    if settings is None or not settings.database.endswith("_test"):
        parser.error("an explicitly isolated *_test database is required")
    connections = TestConnections(settings)
    runtime = build_hub(
        home=args.home,
        settings_file=args.settings,
        port=args.port,
        version="sql-test",
        client_factory=lambda target: listeners.setdefault(target.id, SimulatedListener()),
        sql_connect=connections,
    )
    original = runtime.app.handle

    def handle(method, target, headers, body):
        rejected = runtime.app.validate_request(target, headers)
        if rejected is not None:
            return rejected
        if method == "POST" and target == "/test/sql-outage":
            enabled = json.loads(body)["enabled"]
            if enabled:
                connections.unavailable.set()
            else:
                connections.unavailable.clear()
            return HttpResponse(200, {"enabled": enabled})
        if method == "POST" and target == "/test/audit":
            data = json.loads(body)
            runtime.storage.enqueue("audit", data["id"], {"event": "test.outage", "created_at": time.time()})
            return HttpResponse(200, {"durable": True})
        return original(method, target, headers, body)

    runtime.app.handle = handle
    print(json.dumps({"url": runtime.url, "owner": runtime.storage.owner}), flush=True)
    runtime.serve_forever()


if __name__ == "__main__":
    main()
