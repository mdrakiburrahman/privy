"""HTTP client that sends privy requests to a RelayServer via Azure Relay."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace

import requests

from privy._relay import create_http_send_url, create_sas_token, fqdn
from privy.protocol import (
    DEFAULT_POLL_WAIT_S,
    DEFAULT_TIMEOUT_S,
    ExecRequest,
    ExecResponse,
)

#: Azure Relay fails a request whose listener has not responded within roughly
#: a minute ("the listener did not respond in the required time", HTTP 504).
#: Anything expected to run longer must go through the async job API.
RELAY_RESPONSE_LIMIT_S = 55.0

#: HTTP timeout for the short submit/poll/cancel calls of the async path.
_CONTROL_HTTP_TIMEOUT_S = 60.0

#: Fallback poll cadence, used only when the listener answers a poll instantly
#: instead of long-polling (i.e. an older server). Ramps so quick statements
#: stay quick and long ones do not hammer the relay.
_POLL_BACKOFF_MIN_S = 0.25
_POLL_BACKOFF_MAX_S = 5.0


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
    job_id: str | None = None

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
            job_id=resp.job_id,
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
        async_job: bool | None = None,
    ) -> ExecResult:
        return self.send(
            ExecRequest(kind="python", code=code, mode=mode, timeout_s=timeout_s),
            async_job=async_job,
        )

    def run_bash(
        self,
        code: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        async_job: bool | None = None,
    ) -> ExecResult:
        return self.send(
            ExecRequest(kind="bash", code=code, mode="subprocess", timeout_s=timeout_s),
            async_job=async_job,
        )

    def send(self, request: ExecRequest, *, async_job: bool | None = None) -> ExecResult:
        """Send an :class:`ExecRequest`, synchronously or as a background job.

        ``async_job=None`` (the default) picks the right shape automatically:
        anything allowed to run past :data:`RELAY_RESPONSE_LIMIT_S` goes through
        submit + poll, because a single relay round-trip cannot outlive that.
        Either way the returned :class:`ExecResult` looks identical, so callers
        never have to care.
        """
        if async_job is None:
            async_job = request.timeout_s > RELAY_RESPONSE_LIMIT_S
        if not async_job:
            return self._send(request)
        return self._run_as_job(request)

    def submit(self, request: ExecRequest) -> str:
        """Start ``request`` in the background; returns its ``job_id``."""
        result = self._send(
            replace(request, action="submit"),
            http_timeout_s=_CONTROL_HTTP_TIMEOUT_S,
        )
        if not result.job_id:
            raise RuntimeError(
                "listener did not return a job_id — it is running a privy "
                "version without async job support"
            )
        return result.job_id

    def poll(
        self,
        request: ExecRequest,
        job_id: str,
        *,
        wait_s: float = DEFAULT_POLL_WAIT_S,
    ) -> tuple[str | None, ExecResult]:
        """Long-poll ``job_id``; returns ``(state, result)``."""
        polled = self._send(
            replace(request, action="poll", job_id=job_id, wait_s=wait_s),
            http_timeout_s=_CONTROL_HTTP_TIMEOUT_S,
            return_state=True,
        )
        return polled

    def cancel(self, request: ExecRequest, job_id: str) -> ExecResult:
        """Best-effort cancellation of a running job."""
        return self._send(
            replace(request, action="cancel", job_id=job_id),
            http_timeout_s=_CONTROL_HTTP_TIMEOUT_S,
        )

    # ---- async job driver ---------------------------------------------

    def _run_as_job(self, request: ExecRequest) -> ExecResult:
        submitted = self._send(
            replace(request, action="submit"),
            http_timeout_s=_CONTROL_HTTP_TIMEOUT_S,
        )
        if not submitted.job_id:
            # Older listener: it just ran the code synchronously. The result is
            # already final, so hand it back rather than failing.
            return submitted

        job_id = submitted.job_id
        deadline = time.monotonic() + request.timeout_s + _CONTROL_HTTP_TIMEOUT_S
        wait_s = min(DEFAULT_POLL_WAIT_S, max(1.0, request.timeout_s))
        backoff = _POLL_BACKOFF_MIN_S
        try:
            while True:
                started = time.monotonic()
                state, result = self.poll(request, job_id, wait_s=wait_s)
                if state != "running":
                    return result
                if time.monotonic() > deadline:
                    self.cancel(request, job_id)
                    return ExecResult(
                        exit_code=1,
                        stdout="",
                        stderr=f"job {job_id} exceeded timeout_s={request.timeout_s}\n",
                        stdout_bytes=b"",
                        stderr_bytes=b"",
                        duration_ms=int((time.monotonic() - started) * 1000),
                        timed_out=True,
                        error="timeout",
                        job_id=job_id,
                    )
                # A long-polling listener already blocked for us; only sleep if
                # it came back immediately (older server without long-poll).
                if time.monotonic() - started < 1.0:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, _POLL_BACKOFF_MAX_S)
                else:
                    backoff = _POLL_BACKOFF_MIN_S
        except KeyboardInterrupt:
            self.cancel(request, job_id)
            raise

    # ---- internals -----------------------------------------------------

    def _send(
        self,
        request: ExecRequest,
        *,
        http_timeout_s: float | None = None,
        return_state: bool = False,
    ):
        ns = fqdn(self._namespace)
        token = create_sas_token(ns, self._path, self._keyrule, self._key)
        url = create_http_send_url(ns, self._path, token)

        r = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            data=request.to_json(),
            timeout=http_timeout_s or self._http_timeout_s,
        )
        r.raise_for_status()
        try:
            payload = r.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"server returned non-JSON response (status={r.status_code}): {r.text[:200]!r}"
            ) from exc
        resp = ExecResponse.from_json(json.dumps(payload))
        result = ExecResult.from_response(resp)
        if return_state:
            return resp.state, result
        return result
