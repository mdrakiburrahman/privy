"""HTTP client that sends privy requests to a RelayServer via Azure Relay."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
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
    LISTENER_INFO_KIND,
    PROTOCOL_VERSION,
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

log = logging.getLogger("privy.client")

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


#: ``on_output(stream, data)`` receives ``("stdout" | "stderr", bytes)`` as soon as
#: the listener has produced them.
OutputCallback = Callable[[str, bytes], None]

#: ``on_submit(job_id)`` receives the listener's job id once a job was accepted.
SubmitCallback = Callable[[str], None]

#: HTTP timeout for a ``listener_info`` probe.
_INFO_HTTP_TIMEOUT_S = 15.0
_ERROR_DETAIL_CHARS = 300


class RelayHTTPError(RuntimeError):
    """A Relay request failed before the listener produced a JSON answer.

    ``status_code`` is the HTTP status from Azure Relay or the listener, or
    ``None`` when no response arrived; ``timed_out`` marks a client-side timeout.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        timed_out: bool = False,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.timed_out = timed_out
        self.detail = detail


#: Consecutive failed streaming polls tolerated before giving up.
_STREAM_MAX_POLL_FAILURES = 8


def _deliver(on_output: OutputCallback | None, stdout: bytes, stderr: bytes) -> None:
    if on_output is None:
        return
    if stdout:
        on_output("stdout", stdout)
    if stderr:
        on_output("stderr", stderr)


def _timeout_result(request: ExecRequest, job_id: str, started: float) -> ExecResult:
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
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        return self.send(
            ExecRequest(kind="python", code=code, mode=mode, timeout_s=timeout_s),
            async_job=async_job,
            on_output=on_output,
        )

    def run_bash(
        self,
        code: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        async_job: bool | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        return self.send(
            ExecRequest(kind="bash", code=code, mode="subprocess", timeout_s=timeout_s),
            async_job=async_job,
            on_output=on_output,
        )

    def run_powershell(
        self,
        code: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        async_job: bool | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        return self.send(
            ExecRequest(kind="powershell", code=code, mode="subprocess", timeout_s=timeout_s),
            async_job=async_job,
            on_output=on_output,
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

    def listener_info(self, *, http_timeout_s: float = _INFO_HTTP_TIMEOUT_S) -> dict[str, Any] | None:
        """Describe the listener (host, OS, runtimes) without running any code.

        Returns ``None`` when the listener is reachable but predates this
        request. Raises :class:`RelayHTTPError` when it cannot be reached.
        """
        payload = self._post_json(
            json.dumps({"kind": LISTENER_INFO_KIND, "protocol_version": PROTOCOL_VERSION}),
            http_timeout_s=http_timeout_s,
        )
        if payload.get("kind") == LISTENER_INFO_KIND:
            return payload
        if payload.get("error") == "bad_request":
            return None
        raise RuntimeError("listener returned an unexpected listener_info response")

    def send(
        self,
        request: ExecRequest,
        *,
        async_job: bool | None = None,
        on_output: OutputCallback | None = None,
        on_submit: SubmitCallback | None = None,
    ) -> ExecResult:
        """Send an :class:`ExecRequest`, synchronously or as a background job.

        ``async_job=None`` (the default) picks the right shape automatically:
        anything allowed to run past :data:`RELAY_RESPONSE_LIMIT_S` goes through
        submit + poll, because a single relay round-trip cannot outlive that.
        Either way the returned :class:`ExecResult` looks identical, so callers
        never have to care.

        ``on_output`` streams stdout/stderr chunks while the command runs; it
        needs the job transport, so it implies ``async_job=True``. Every output
        byte is delivered through the callback exactly once (against a listener
        without streaming support, all of it arrives at completion). The
        returned result still holds the complete output.

        ``on_submit`` receives the listener's job id as soon as the job is
        accepted, so another thread can :meth:`cancel` it. It also implies
        ``async_job=True``.
        """
        for name, callback in (("on_output", on_output), ("on_submit", on_submit)):
            if callback is not None:
                if async_job is False:
                    raise ValueError(f"{name} requires the async job transport (async_job=False given)")
                async_job = True
        if async_job is None:
            async_job = request.timeout_s > RELAY_RESPONSE_LIMIT_S
        if not async_job:
            return self._send(request)
        return self._run_as_job(request, on_output, on_submit)

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

    def _run_as_job(
        self,
        request: ExecRequest,
        on_output: OutputCallback | None = None,
        on_submit: SubmitCallback | None = None,
    ) -> ExecResult:
        submitted = self._send_raw(
            replace(request, action="submit", stream=on_output is not None),
            http_timeout_s=_CONTROL_HTTP_TIMEOUT_S,
        )
        if not submitted.job_id:
            # Older listener: it just ran the code synchronously. The result is
            # already final, so hand it back rather than failing.
            result = ExecResult.from_response(submitted)
            _deliver(on_output, result.stdout_bytes, result.stderr_bytes)
            return result

        job_id = submitted.job_id
        if on_submit is not None:
            on_submit(job_id)
        if on_output is not None and submitted.stream:
            return self._stream_job(request, job_id, on_output)

        deadline = time.monotonic() + request.timeout_s + _CONTROL_HTTP_TIMEOUT_S
        wait_s = min(DEFAULT_POLL_WAIT_S, max(1.0, request.timeout_s))
        backoff = _POLL_BACKOFF_MIN_S
        try:
            while True:
                started = time.monotonic()
                state, result = self.poll(request, job_id, wait_s=wait_s)
                if state != "running":
                    _deliver(on_output, result.stdout_bytes, result.stderr_bytes)
                    return result
                if time.monotonic() > deadline:
                    self.cancel(request, job_id)
                    return _timeout_result(request, job_id, started)
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

    def _stream_job(self, request: ExecRequest, job_id: str, on_output: OutputCallback) -> ExecResult:
        """Poll with a watermark, delivering each chunk once; retries are idempotent."""
        deadline = time.monotonic() + request.timeout_s + _CONTROL_HTTP_TIMEOUT_S
        wait_s = min(DEFAULT_POLL_WAIT_S, max(1.0, request.timeout_s))
        stdout_offset = stderr_offset = 0
        stdout_all = bytearray()
        stderr_all = bytearray()
        failures = 0
        try:
            while True:
                started = time.monotonic()
                try:
                    resp = self._send_raw(
                        replace(
                            request,
                            action="poll",
                            job_id=job_id,
                            wait_s=wait_s,
                            stdout_offset=stdout_offset,
                            stderr_offset=stderr_offset,
                        ),
                        http_timeout_s=_CONTROL_HTTP_TIMEOUT_S,
                    )
                except RuntimeError:
                    # Same offsets next time, so a lost response loses nothing.
                    failures += 1
                    if time.monotonic() > deadline or failures > _STREAM_MAX_POLL_FAILURES:
                        raise
                    time.sleep(min(_POLL_BACKOFF_MIN_S * 2**failures, _POLL_BACKOFF_MAX_S))
                    continue
                failures = 0

                if resp.stdout_offset is None or resp.stderr_offset is None:
                    # Listener does not track offsets: treat as an ordinary job poll.
                    if resp.state == "running":
                        time.sleep(_POLL_BACKOFF_MIN_S)
                        continue
                    result = ExecResult.from_response(resp)
                    _deliver(on_output, result.stdout_bytes, result.stderr_bytes)
                    return result

                out, err = resp.stdout, resp.stderr
                if out:
                    stdout_all += out
                    on_output("stdout", out)
                if err:
                    stderr_all += err
                    on_output("stderr", err)
                stdout_offset, stderr_offset = resp.stdout_offset, resp.stderr_offset

                if resp.state != "running":
                    final = ExecResult.from_response(resp)
                    final.stdout_bytes = bytes(stdout_all)
                    final.stderr_bytes = bytes(stderr_all)
                    final.stdout = final.stdout_bytes.decode("utf-8", "replace")
                    final.stderr = final.stderr_bytes.decode("utf-8", "replace")
                    return final
                if time.monotonic() > deadline:
                    self.cancel(request, job_id)
                    result = _timeout_result(request, job_id, started)
                    result.stdout_bytes = bytes(stdout_all)
                    result.stderr_bytes = bytes(stderr_all)
                    result.stdout = result.stdout_bytes.decode("utf-8", "replace")
                    return result
                # Output was waiting (full chunk): fetch the next one right away.
                # An idle poll already blocked server-side; an instant empty
                # answer would otherwise spin.
                if not out and not err and time.monotonic() - started < 0.05:
                    time.sleep(_POLL_BACKOFF_MIN_S)
        except KeyboardInterrupt:
            self.cancel(request, job_id)
            raise

    # ---- internals -----------------------------------------------------

    def _send_raw(self, request: ExecRequest, *, http_timeout_s: float | None = None) -> ExecResponse:
        started = time.monotonic()
        payload = self._post_json(
            request.to_json(),
            http_timeout_s=http_timeout_s,
        )
        response = ExecResponse.from_json(json.dumps(payload))
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "relay %s/%s %s%s -> %s exit=%s stdout+%d stderr+%d (%d ms)",
                self._credential.namespace,
                self._credential.path,
                request.action,
                f" job={request.job_id}" if request.job_id else f" {request.kind}",
                response.state or "done",
                response.exit_code,
                len(response.stdout_b64) * 3 // 4,
                len(response.stderr_b64) * 3 // 4,
                (time.monotonic() - started) * 1000,
            )
        return response

    def _send(
        self,
        request: ExecRequest,
        *,
        http_timeout_s: float | None = None,
        return_state: bool = False,
    ):
        resp = self._send_raw(request, http_timeout_s=http_timeout_s)
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
            failed = getattr(exc, "response", None)
            detail = ""
            if failed is not None:
                try:
                    detail = redact_relay_secrets(failed.text[:_ERROR_DETAIL_CHARS])
                except Exception:  # noqa: BLE001 - diagnostics only
                    detail = ""
            raise RelayHTTPError(
                f"relay request failed: {message}",
                status_code=None if failed is None else failed.status_code,
                timed_out=isinstance(exc, requests.Timeout),
                detail=detail,
            ) from None
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
