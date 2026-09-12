"""HTTP client that sends privy requests to a RelayServer via Azure Relay."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

import requests

from privy._relay import (
    DEFAULT_TOKEN_TTL_SECONDS,
    RelayCredential,
    TokenProvider,
    create_http_send_url,
    redact_relay_secrets,
)
from privy.batch import (
    DEFAULT_MAX_PARALLEL,
    BatchResult,
    CommandSpec,
)
from privy.batch import (
    run_many as run_command_batch,
)
from privy.protocol import (
    DEFAULT_POLL_WAIT_S,
    DEFAULT_TIMEOUT_S,
    ExecRequest,
    ExecResponse,
)
from privy.transfer import (
    DEFAULT_CHUNK_SIZE,
    ProgressCallback,
    TransferResult,
)
from privy.transfer import (
    download_file as download_over_relay,
)
from privy.transfer import (
    upload_file as upload_over_relay,
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
        keyrule: str | None = None,
        key: str | None = None,
        token: TokenProvider | None = None,
        ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
        http_timeout_s: float = DEFAULT_TIMEOUT_S + 30.0,
    ) -> None:
        self._credential = RelayCredential(
            namespace=namespace,
            path=path,
            keyrule=keyrule,
            key=key,
            token=token,
            ttl_seconds=ttl_seconds,
        )
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

    def upload_file(
        self,
        local_path: str | os.PathLike[str],
        remote_path: str | os.PathLike[str],
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overwrite: bool = False,
        progress: ProgressCallback | None = None,
    ) -> TransferResult:
        """Upload one file to the listener, resuming a matching partial upload."""
        return upload_over_relay(
            self._post_json,
            local_path,
            remote_path,
            chunk_size=chunk_size,
            overwrite=overwrite,
            progress=progress,
        )

    def download_file(
        self,
        remote_path: str | os.PathLike[str],
        local_path: str | os.PathLike[str],
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overwrite: bool = False,
        progress: ProgressCallback | None = None,
    ) -> TransferResult:
        """Download one file from the listener, resuming a matching partial download."""
        return download_over_relay(
            self._post_json,
            remote_path,
            local_path,
            chunk_size=chunk_size,
            overwrite=overwrite,
            progress=progress,
        )

    def run_many(
        self,
        commands: Iterable[CommandSpec],
        *,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
    ) -> BatchResult:
        """Run commands according to their dependency graph."""
        return run_command_batch(self, commands, max_parallel=max_parallel)

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
                "listener did not return a job_id — it is running a privy version without async job support"
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
        payload = self._post_json(
            request.to_json(),
            http_timeout_s=http_timeout_s,
        )
        resp = ExecResponse.from_json(json.dumps(payload))
        result = ExecResult.from_response(resp)
        if return_state:
            return resp.state, result
        return result

    def _post_json(
        self,
        payload: str,
        *,
        http_timeout_s: float | None = None,
    ) -> dict[str, Any]:
        token, _ = self._credential.resolve()
        url = create_http_send_url(
            self._credential.namespace,
            self._credential.path,
            token,
        )
        try:
            response = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                data=payload,
                timeout=http_timeout_s or self._http_timeout_s,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            message = redact_relay_secrets(str(exc))
            raise RuntimeError(f"relay request failed: {message}") from None
        try:
            decoded = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"server returned non-JSON response (status={response.status_code}): "
                f"{redact_relay_secrets(response.text[:200])!r}"
            ) from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"server returned {type(decoded).__name__}, expected a JSON object")
        return decoded
