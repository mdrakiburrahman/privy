"""HTTP client that sends privy requests to a RelayServer via Azure Relay."""

from __future__ import annotations

import json
from dataclasses import dataclass

import requests

from privy._relay import create_http_send_url, create_sas_token, fqdn
from privy.protocol import DEFAULT_TIMEOUT_S, ExecRequest, ExecResponse


@dataclass
class ExecResult:
    """Client-side view of an ExecResponse with text-decoded output."""

    exit_code: int
    stdout: str
    stderr: str
    stdout_bytes: bytes
    stderr_bytes: bytes
    duration_ms: int
    timed_out: bool
    error: str | None

    @classmethod
    def from_response(cls, resp: ExecResponse) -> ExecResult:
        stdout_bytes = resp.stdout
        stderr_bytes = resp.stderr
        return cls(
            exit_code=resp.exit_code,
            stdout=stdout_bytes.decode("utf-8", "replace"),
            stderr=stderr_bytes.decode("utf-8", "replace"),
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            duration_ms=resp.duration_ms,
            timed_out=resp.timed_out,
            error=resp.error,
        )

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class RelayClient:
    """Send execution requests to a remote :class:`RelayServer`.

    Credentials are passed in as constructor arguments — the client never
    reads files or environment variables itself.

    Example::

        client = RelayClient(namespace="myns-relay", path="demo",
                             keyrule="demo-listen-send", key="...")
        r = client.run_bash("pip install pandas==2.2.*")
        print(r.stdout, r.exit_code)
        r = client.run_python("import pandas as pd; print(pd.__version__)")
    """

    def __init__(
        self,
        *,
        namespace: str,
        path: str,
        keyrule: str,
        key: str,
        http_timeout_s: float = DEFAULT_TIMEOUT_S + 30.0,
    ) -> None:
        if not all([namespace, path, keyrule, key]):
            raise ValueError("namespace, path, keyrule and key are all required")
        self._namespace = namespace
        self._path = path
        self._keyrule = keyrule
        self._key = key
        self._http_timeout_s = http_timeout_s

    # ---- public API ----------------------------------------------------

    def run_python(
        self,
        code: str,
        *,
        mode: str = "subprocess",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> ExecResult:
        return self._send(ExecRequest(kind="python", code=code, mode=mode, timeout_s=timeout_s))

    def run_bash(
        self,
        code: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> ExecResult:
        return self._send(ExecRequest(kind="bash", code=code, mode="subprocess", timeout_s=timeout_s))

    def send(self, request: ExecRequest) -> ExecResult:
        """Low-level: send an already-constructed :class:`ExecRequest`."""
        return self._send(request)

    # ---- internals -----------------------------------------------------

    def _send(self, request: ExecRequest) -> ExecResult:
        ns = fqdn(self._namespace)
        token = create_sas_token(ns, self._path, self._keyrule, self._key)
        url = create_http_send_url(ns, self._path, token)

        r = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            data=request.to_json(),
            timeout=self._http_timeout_s,
        )
        r.raise_for_status()
        try:
            payload = r.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"server returned non-JSON response (status={r.status_code}): {r.text[:200]!r}"
            ) from exc
        resp = ExecResponse.from_json(json.dumps(payload))
        return ExecResult.from_response(resp)
