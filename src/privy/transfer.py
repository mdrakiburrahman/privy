"""Resumable, integrity-checked file transfer over privy's Relay transport."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

FILE_TRANSFER_KIND = "file_transfer"
DEFAULT_CHUNK_SIZE = 1024 * 1024
MAX_CHUNK_SIZE = 2 * 1024 * 1024

TransferAction = Literal[
    "upload_start",
    "upload_chunk",
    "upload_complete",
    "download_start",
    "download_hash",
    "download_chunk",
]
TransferDirection = Literal["upload", "download"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()
_UPLOAD_VERIFIERS: dict[str, _UploadVerifier] = {}
_UPLOAD_VERIFIERS_GUARD = threading.Lock()
_DOWNLOAD_SESSIONS: dict[str, _DownloadSession] = {}
_DOWNLOAD_SESSIONS_GUARD = threading.Lock()
_DOWNLOAD_SESSION_RETENTION_S = 60 * 60
_MAX_DOWNLOAD_SESSIONS = 1024
log = logging.getLogger("privy.transfer")


class TransferError(RuntimeError):
    """A file transfer failed locally or on the remote listener."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class _UploadVerifier:
    stat_key: tuple[int, int, int, int]
    verification_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    offset: int = 0
    hasher: Any = field(default_factory=hashlib.sha256)
    last_used_at: float = field(default_factory=time.monotonic)


@dataclass
class _DownloadSession:
    source: Path
    snapshot_id: str
    stat_key: tuple[int, int, int, int]
    size: int
    offset: int = 0
    hasher: Any = field(default_factory=hashlib.sha256)
    last_request: tuple[str, int, int] | None = None
    final_digest: str | None = None
    last_used_at: float = field(default_factory=time.monotonic)


@dataclass
class TransferRequest:
    action: TransferAction
    path: str
    transfer_id: str | None = None
    offset: int = 0
    size: int | None = None
    sha256: str | None = None
    data_b64: str = ""
    overwrite: bool = False
    chunk_size: int = DEFAULT_CHUNK_SIZE
    kind: str = FILE_TRANSFER_KIND

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> TransferRequest:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TransferError("bad_request", "transfer request must be valid JSON") from exc
        if not isinstance(obj, dict) or obj.get("kind") != FILE_TRANSFER_KIND:
            raise TransferError("bad_request", "not a file transfer request")

        action = obj.get("action")
        actions = {
            "upload_start",
            "upload_chunk",
            "upload_complete",
            "download_start",
            "download_hash",
            "download_chunk",
        }
        if action not in actions:
            raise TransferError("bad_request", f"invalid transfer action: {action!r}")
        path = obj.get("path")
        if not isinstance(path, str) or not path:
            raise TransferError("bad_request", "transfer path must be a non-empty string")

        offset = _as_non_negative_int(obj.get("offset", 0), "offset")
        chunk_size = _as_non_negative_int(obj.get("chunk_size", DEFAULT_CHUNK_SIZE), "chunk_size")
        if not 0 < chunk_size <= MAX_CHUNK_SIZE:
            raise TransferError(
                "bad_request",
                f"chunk_size must be between 1 and {MAX_CHUNK_SIZE} bytes",
            )

        size_value = obj.get("size")
        size = None if size_value is None else _as_non_negative_int(size_value, "size")
        digest = obj.get("sha256")
        if digest is not None and (not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)):
            raise TransferError("bad_request", "sha256 must be 64 lowercase hexadecimal characters")
        transfer_id = obj.get("transfer_id")
        if transfer_id is not None and (not isinstance(transfer_id, str) or not transfer_id):
            raise TransferError("bad_request", "transfer_id must be a non-empty string")
        data_b64 = obj.get("data_b64", "")
        if not isinstance(data_b64, str):
            raise TransferError("bad_request", "data_b64 must be a string")
        overwrite = obj.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise TransferError("bad_request", "overwrite must be a boolean")

        if action.startswith("upload_") and (size is None or digest is None):
            raise TransferError("bad_request", f"{action} requires size and sha256")
        if (
            action
            in (
                "upload_chunk",
                "upload_complete",
                "download_hash",
                "download_chunk",
            )
            and not transfer_id
        ):
            raise TransferError("bad_request", f"{action} requires transfer_id")
        if action == "upload_chunk" and not data_b64:
            raise TransferError("bad_request", "upload_chunk requires data_b64")

        return cls(
            action=action,
            path=path,
            transfer_id=transfer_id,
            offset=offset,
            size=size,
            sha256=digest,
            data_b64=data_b64,
            overwrite=overwrite,
            chunk_size=chunk_size,
        )


@dataclass
class TransferResponse:
    ok: bool
    action: str
    transfer_id: str | None = None
    snapshot_id: str | None = None
    offset: int = 0
    size: int = 0
    sha256: str | None = None
    data_b64: str = ""
    complete: bool = False
    error: str | None = None
    message: str | None = None
    kind: str = FILE_TRANSFER_KIND

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes | dict[str, Any]) -> TransferResponse:
        if isinstance(raw, dict):
            obj = raw
        else:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise TransferError("bad_response", "listener returned invalid transfer JSON") from exc
        if not isinstance(obj, dict) or obj.get("kind") != FILE_TRANSFER_KIND:
            raise TransferError("bad_response", "listener returned a non-transfer response")
        ok = obj.get("ok")
        complete = obj.get("complete", False)
        data_b64 = obj.get("data_b64", "") or ""
        digest = obj.get("sha256")
        transfer_id = obj.get("transfer_id")
        snapshot_id = obj.get("snapshot_id")
        if not isinstance(ok, bool) or not isinstance(complete, bool):
            raise TransferError("bad_response", "listener returned invalid transfer status fields")
        if not isinstance(data_b64, str):
            raise TransferError("bad_response", "listener returned non-string file data")
        if digest is not None and (not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)):
            raise TransferError("bad_response", "listener returned an invalid SHA-256 digest")
        for name, value in (("transfer_id", transfer_id), ("snapshot_id", snapshot_id)):
            if value is not None and (not isinstance(value, str) or not value):
                raise TransferError("bad_response", f"listener returned an invalid {name}")
        response = cls(
            ok=ok,
            action=str(obj.get("action", "")),
            transfer_id=transfer_id,
            snapshot_id=snapshot_id,
            offset=_as_non_negative_int(obj.get("offset", 0), "offset"),
            size=_as_non_negative_int(obj.get("size", 0), "size"),
            sha256=digest,
            data_b64=data_b64,
            complete=complete,
            error=obj.get("error"),
            message=obj.get("message"),
        )
        if not response.ok:
            raise TransferError(
                response.error or "transfer_failed",
                response.message or "remote file transfer failed",
            )
        return response


@dataclass(frozen=True)
class TransferResult:
    direction: TransferDirection
    source: str
    destination: str
    size: int
    sha256: str
    transfer_id: str
    resumed_from: int = 0

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


PostJson = Callable[[str], dict[str, Any]]
ProgressCallback = Callable[[int, int], None]


def handle_transfer_request(raw: str | bytes) -> TransferResponse:
    """Handle one bounded transfer operation on the listener."""
    try:
        request = TransferRequest.from_json(raw)
        if request.action == "upload_start":
            return _upload_start(request)
        if request.action == "upload_chunk":
            return _upload_chunk(request)
        if request.action == "upload_complete":
            return _upload_complete(request)
        if request.action == "download_start":
            return _download_start(request)
        if request.action == "download_hash":
            return _download_advance(request, include_data=False)
        return _download_advance(request, include_data=True)
    except TransferError as exc:
        return TransferResponse(
            ok=False,
            action=_request_action(raw),
            error=exc.code,
            message=str(exc),
        )
    except OSError as exc:
        return TransferResponse(
            ok=False,
            action=_request_action(raw),
            error="filesystem_error",
            message=str(exc),
        )


def upload_file(
    post_json: PostJson,
    local_path: str | os.PathLike[str],
    remote_path: str | os.PathLike[str],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
) -> TransferResult:
    """Upload one local file, resuming a matching remote partial file."""
    _validate_chunk_size(chunk_size)
    source = Path(local_path).expanduser().resolve()
    if not source.is_file():
        raise TransferError("source_not_file", f"local source is not a regular file: {source}")
    size = source.stat().st_size
    digest = _sha256_file(source)
    destination = str(remote_path)

    start = _send(
        post_json,
        TransferRequest(
            action="upload_start",
            path=destination,
            size=size,
            sha256=digest,
            overwrite=overwrite,
            chunk_size=chunk_size,
        ),
    )
    if not start.transfer_id:
        raise TransferError("bad_response", "listener omitted upload transfer_id")
    if start.offset > size:
        raise TransferError("bad_response", "listener upload offset exceeds source size")
    resumed_from = start.offset
    if start.complete:
        if start.offset != size or start.sha256 != digest:
            raise TransferError("bad_response", "listener returned inconsistent completed upload metadata")
        return TransferResult(
            direction="upload",
            source=str(source),
            destination=destination,
            size=size,
            sha256=digest,
            transfer_id=start.transfer_id,
            resumed_from=resumed_from,
        )
    offset = start.offset
    if progress:
        progress(offset, size)

    with source.open("rb") as stream:
        stream.seek(offset)
        while offset < size:
            data = stream.read(min(chunk_size, size - offset))
            if not data:
                raise TransferError("source_changed", "local source ended before its original size")
            response = _send(
                post_json,
                TransferRequest(
                    action="upload_chunk",
                    path=destination,
                    transfer_id=start.transfer_id,
                    offset=offset,
                    size=size,
                    sha256=digest,
                    data_b64=base64.b64encode(data).decode("ascii"),
                    overwrite=overwrite,
                    chunk_size=chunk_size,
                ),
            )
            expected = offset + len(data)
            if response.offset != expected:
                raise TransferError(
                    "bad_response",
                    f"listener returned upload offset {response.offset}, expected {expected}",
                )
            offset = response.offset
            if progress:
                progress(offset, size)

    verified_offset = 0
    verification_id: str | None = None
    while True:
        completed = _send(
            post_json,
            TransferRequest(
                action="upload_complete",
                path=destination,
                transfer_id=start.transfer_id,
                offset=offset,
                size=size,
                sha256=digest,
                overwrite=overwrite,
                chunk_size=chunk_size,
            ),
        )
        if completed.complete:
            if completed.sha256 != digest:
                raise TransferError("bad_response", "listener confirmed upload with the wrong digest")
            break
        if completed.snapshot_id != verification_id:
            verification_id = completed.snapshot_id
            verified_offset = 0
        if completed.offset <= verified_offset or completed.offset > size:
            raise TransferError("bad_response", "listener upload verification made no progress")
        verified_offset = completed.offset
    return TransferResult(
        direction="upload",
        source=str(source),
        destination=destination,
        size=size,
        sha256=digest,
        transfer_id=start.transfer_id,
        resumed_from=resumed_from,
    )


def download_file(
    post_json: PostJson,
    remote_path: str | os.PathLike[str],
    local_path: str | os.PathLike[str],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
) -> TransferResult:
    """Download one remote file, resuming a matching local partial file."""
    _validate_chunk_size(chunk_size)
    destination = Path(local_path).expanduser().resolve()
    if destination.exists() and not overwrite:
        raise TransferError("destination_exists", f"local destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise TransferError(
            "missing_parent", f"local destination parent does not exist: {destination.parent}"
        )

    remote = str(remote_path)
    metadata = _send(
        post_json,
        TransferRequest(
            action="download_start",
            path=remote,
            chunk_size=chunk_size,
        ),
    )
    if not metadata.transfer_id or not metadata.snapshot_id:
        raise TransferError("bad_response", "listener omitted download metadata")

    partial = _download_partial_path(destination, metadata.snapshot_id)
    if partial.exists() and not partial.is_file():
        raise TransferError("partial_not_file", f"local partial path is not a regular file: {partial}")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > metadata.size:
        partial.unlink()
        offset = 0
    resumed_from = offset
    if progress:
        progress(offset, metadata.size)

    hash_offset = 0
    expected_digest = metadata.sha256
    while hash_offset < offset:
        response = _send(
            post_json,
            TransferRequest(
                action="download_hash",
                path=remote,
                transfer_id=metadata.transfer_id,
                offset=hash_offset,
                chunk_size=min(chunk_size, offset - hash_offset),
            ),
        )
        if response.offset <= hash_offset or response.offset > offset:
            raise TransferError("bad_response", "listener download verification made no progress")
        hash_offset = response.offset
        if response.sha256:
            expected_digest = response.sha256

    mode = "ab" if offset else "wb"
    with partial.open(mode) as stream:
        while offset < metadata.size:
            response = _send(
                post_json,
                TransferRequest(
                    action="download_chunk",
                    path=remote,
                    transfer_id=metadata.transfer_id,
                    offset=offset,
                    chunk_size=chunk_size,
                ),
            )
            try:
                data = base64.b64decode(response.data_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise TransferError("bad_response", "listener returned invalid base64 file data") from exc
            if not data:
                raise TransferError("bad_response", "listener returned an empty chunk before end of file")
            if len(data) > chunk_size or offset + len(data) > metadata.size:
                raise TransferError("bad_response", "listener returned an oversized download chunk")
            stream.write(data)
            offset += len(data)
            if response.offset != offset:
                raise TransferError(
                    "bad_response",
                    f"listener returned download offset {response.offset}, expected {offset}",
                )
            if progress:
                progress(offset, metadata.size)
            if response.sha256:
                expected_digest = response.sha256
        stream.flush()
        os.fsync(stream.fileno())

    if not expected_digest:
        raise TransferError("bad_response", "listener omitted the completed download digest")
    digest = _sha256_file(partial)
    if digest != expected_digest:
        partial.unlink(missing_ok=True)
        raise TransferError(
            "checksum_mismatch",
            f"download SHA-256 mismatch: expected {expected_digest}, got {digest}",
        )
    _commit_partial(partial, destination, overwrite=overwrite, location="local")
    return TransferResult(
        direction="download",
        source=remote,
        destination=str(destination),
        size=metadata.size,
        sha256=digest,
        transfer_id=metadata.snapshot_id,
        resumed_from=resumed_from,
    )


def _upload_start(request: TransferRequest) -> TransferResponse:
    assert request.size is not None and request.sha256 is not None
    destination = _resolve_path(request.path)
    transfer_id, partial = _upload_identity(destination, request.size, request.sha256)
    with _path_lock(destination):
        _reap_upload_verifiers()
        if _is_completed_upload(destination, transfer_id, request.size, request.sha256):
            return _completed_upload_response(request, transfer_id)
        _validate_upload_destination(destination, request.overwrite)
        if partial.exists() and not partial.is_file():
            raise TransferError("partial_not_file", f"upload partial is not a regular file: {partial}")
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > request.size:
            partial.unlink()
            offset = 0
        if not partial.exists():
            partial.touch(exist_ok=False)
        _drop_upload_verifier(transfer_id)
        return TransferResponse(
            ok=True,
            action=request.action,
            transfer_id=transfer_id,
            offset=offset,
            size=request.size,
            sha256=request.sha256,
            complete=False,
        )


def _upload_chunk(request: TransferRequest) -> TransferResponse:
    assert request.size is not None and request.sha256 is not None and request.transfer_id is not None
    if len(request.data_b64) > ((MAX_CHUNK_SIZE + 2) // 3) * 4:
        raise TransferError("chunk_too_large", f"upload chunk exceeds {MAX_CHUNK_SIZE} bytes")
    try:
        data = base64.b64decode(request.data_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TransferError("bad_base64", "upload chunk is not valid base64") from exc
    if not data or len(data) > MAX_CHUNK_SIZE:
        raise TransferError("chunk_too_large", f"upload chunk must contain 1-{MAX_CHUNK_SIZE} bytes")

    destination = _resolve_path(request.path)
    transfer_id, partial = _upload_identity(destination, request.size, request.sha256)
    if request.transfer_id != transfer_id:
        raise TransferError("transfer_changed", "upload metadata does not match transfer_id")
    with _path_lock(destination):
        if _is_completed_upload(destination, transfer_id, request.size, request.sha256):
            return _completed_upload_response(request, transfer_id)
        _validate_upload_destination(destination, request.overwrite)
        if not partial.is_file():
            raise TransferError("missing_partial", "upload was not initialized or its partial file is gone")
        current = partial.stat().st_size
        expected = request.offset + len(data)
        if expected > request.size:
            raise TransferError("size_exceeded", "upload chunk exceeds declared file size")
        if current == expected:
            with partial.open("rb") as stream:
                stream.seek(request.offset)
                if stream.read(len(data)) != data:
                    raise TransferError("offset_conflict", "retry data differs from the stored upload chunk")
        elif current == request.offset:
            with partial.open("ab") as stream:
                stream.write(data)
                stream.flush()
            _drop_upload_verifier(transfer_id)
        else:
            raise TransferError(
                "offset_mismatch",
                f"upload offset is {current}, client sent {request.offset}",
            )
        return TransferResponse(
            ok=True,
            action=request.action,
            transfer_id=transfer_id,
            offset=expected,
            size=request.size,
            sha256=request.sha256,
            complete=False,
        )


def _upload_complete(request: TransferRequest) -> TransferResponse:
    assert request.size is not None and request.sha256 is not None and request.transfer_id is not None
    destination = _resolve_path(request.path)
    transfer_id, partial = _upload_identity(destination, request.size, request.sha256)
    if request.transfer_id != transfer_id:
        raise TransferError("transfer_changed", "upload metadata does not match transfer_id")
    with _path_lock(destination):
        if _is_completed_upload(destination, transfer_id, request.size, request.sha256):
            return _completed_upload_response(request, transfer_id)
        _validate_upload_destination(destination, request.overwrite)
        if not partial.is_file():
            raise TransferError("missing_partial", "upload was not initialized or its partial file is gone")
        actual_size = partial.stat().st_size
        if actual_size != request.size:
            raise TransferError(
                "incomplete_upload",
                f"upload has {actual_size} of {request.size} bytes",
            )
        stat = partial.stat()
        stat_key = _stat_key(stat)
        verifier = _upload_verifier(transfer_id, stat_key)
        with partial.open("rb") as stream:
            stream.seek(verifier.offset)
            block = stream.read(request.chunk_size)
        verifier.hasher.update(block)
        verifier.offset += len(block)
        verifier.last_used_at = time.monotonic()
        if verifier.offset < request.size:
            return TransferResponse(
                ok=True,
                action=request.action,
                transfer_id=transfer_id,
                snapshot_id=verifier.verification_id,
                offset=verifier.offset,
                size=request.size,
                complete=False,
            )
        digest = verifier.hasher.hexdigest()
        if digest != request.sha256:
            _drop_upload_verifier(transfer_id)
            partial.unlink(missing_ok=True)
            raise TransferError(
                "checksum_mismatch",
                f"upload SHA-256 mismatch: expected {request.sha256}, got {digest}",
            )
        with partial.open("rb") as stream:
            os.fsync(stream.fileno())
        marker = _write_completion_marker(
            destination,
            transfer_id,
            request.size,
            digest,
            stat_key,
        )
        try:
            _commit_partial(partial, destination, overwrite=request.overwrite, location="remote")
        except Exception:
            marker.unlink(missing_ok=True)
            raise
        _drop_upload_verifier(transfer_id)
        _remove_stale_completion_markers(destination, keep=marker)
        return _completed_upload_response(request, transfer_id)


def _download_start(request: TransferRequest) -> TransferResponse:
    source = _resolve_path(request.path)
    with _path_lock(source):
        _reap_download_sessions()
        stat = _validate_download_source(source)
        snapshot_id = _download_identity(source, stat)
        transfer_id = uuid.uuid4().hex
        session = _DownloadSession(
            source=source,
            snapshot_id=snapshot_id,
            stat_key=_stat_key(stat),
            size=stat.st_size,
        )
        digest = session.hasher.hexdigest() if stat.st_size == 0 else None
        response = TransferResponse(
            ok=True,
            action=request.action,
            transfer_id=transfer_id,
            snapshot_id=snapshot_id,
            offset=0,
            size=stat.st_size,
            sha256=digest,
            complete=stat.st_size == 0,
        )
        _store_download_session(transfer_id, session)
        return response


def _download_advance(request: TransferRequest, *, include_data: bool) -> TransferResponse:
    assert request.transfer_id is not None
    source = _resolve_path(request.path)
    with _DOWNLOAD_SESSIONS_GUARD:
        session = _DOWNLOAD_SESSIONS.get(request.transfer_id)
    if session is None:
        raise TransferError("missing_transfer", "download session is missing or expired")
    if source != session.source:
        raise TransferError("transfer_changed", "download path does not match transfer_id")
    with _path_lock(source):
        stat = _validate_download_source(source)
        if _stat_key(stat) != session.stat_key:
            raise TransferError("source_changed", "remote source changed during download")
        request_key = (request.action, request.offset, request.chunk_size)
        if request_key == session.last_request:
            return _replay_download(session, request, include_data=include_data)
        if request.offset != session.offset:
            raise TransferError(
                "offset_mismatch",
                f"download offset is {session.offset}, client sent {request.offset}",
            )
        with source.open("rb") as stream:
            if _stat_key(os.fstat(stream.fileno())) != session.stat_key:
                raise TransferError("source_changed", "remote source changed during download")
            stream.seek(request.offset)
            data = stream.read(min(request.chunk_size, MAX_CHUNK_SIZE))
            if _stat_key(os.fstat(stream.fileno())) != session.stat_key:
                raise TransferError("source_changed", "remote source changed during download")
        if _stat_key(source.stat()) != session.stat_key:
            raise TransferError("source_changed", "remote source changed during download")
        if not data and session.offset < session.size:
            raise TransferError("source_changed", "remote source ended before its original size")
        session.hasher.update(data)
        session.offset += len(data)
        session.last_used_at = time.monotonic()
        complete = session.offset == session.size
        digest = session.hasher.hexdigest() if complete else None
        session.final_digest = digest
        response = TransferResponse(
            ok=True,
            action=request.action,
            transfer_id=request.transfer_id,
            snapshot_id=session.snapshot_id,
            offset=session.offset,
            size=session.size,
            sha256=digest,
            data_b64=base64.b64encode(data).decode("ascii") if include_data else "",
            complete=complete,
        )
        session.last_request = request_key
        return response


def _send(post_json: PostJson, request: TransferRequest) -> TransferResponse:
    return TransferResponse.from_json(post_json(request.to_json()))


def _as_non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TransferError("bad_request", f"{name} must be an integer")
    if value < 0:
        raise TransferError("bad_request", f"{name} must be non-negative")
    return value


def _validate_chunk_size(chunk_size: int) -> None:
    if isinstance(chunk_size, bool) or not 0 < chunk_size <= MAX_CHUNK_SIZE:
        raise TransferError(
            "invalid_chunk_size",
            f"chunk_size must be between 1 and {MAX_CHUNK_SIZE} bytes",
        )


def _resolve_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _validate_upload_destination(destination: Path, overwrite: bool) -> None:
    if not destination.parent.is_dir():
        raise TransferError(
            "missing_parent",
            f"remote destination parent does not exist: {destination.parent}",
        )
    if destination.exists():
        if not destination.is_file():
            raise TransferError(
                "destination_not_file",
                f"remote destination is not a regular file: {destination}",
            )
        if not overwrite:
            raise TransferError(
                "destination_exists",
                f"remote destination already exists: {destination}",
            )


def _validate_download_source(source: Path) -> os.stat_result:
    if not source.is_file():
        raise TransferError("source_not_file", f"remote source is not a regular file: {source}")
    return source.stat()


def _upload_identity(destination: Path, size: int, digest: str) -> tuple[str, Path]:
    seed = f"{destination}\0{size}\0{digest}".encode()
    transfer_id = hashlib.sha256(seed).hexdigest()
    partial = destination.parent / f".{destination.name}.privy-upload-{transfer_id[:16]}.part"
    return transfer_id, partial


def _download_identity(source: Path, stat: os.stat_result) -> str:
    return hashlib.sha256(f"{source}\0{_stat_key(stat)}".encode()).hexdigest()


def _completion_marker_path(destination: Path, transfer_id: str) -> Path:
    return destination.parent / f".{destination.name}.privy-upload-{transfer_id[:16]}.complete"


def _is_completed_upload(destination: Path, transfer_id: str, size: int, digest: str) -> bool:
    marker = _completion_marker_path(destination, transfer_id)
    if not destination.is_file() or not marker.is_file():
        return False
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        marker_stat = tuple(value["stat_key"])
        destination_stat = _stat_key(destination.stat())
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False
    return (
        value.get("transfer_id") == transfer_id
        and value.get("size") == size
        and value.get("sha256") == digest
        and marker_stat == destination_stat
    )


def _write_completion_marker(
    destination: Path,
    transfer_id: str,
    size: int,
    digest: str,
    stat_key: tuple[int, int, int, int],
) -> Path:
    marker = _completion_marker_path(destination, transfer_id)
    temporary = marker.with_name(f"{marker.name}.{uuid.uuid4().hex}.tmp")
    payload = {
        "transfer_id": transfer_id,
        "size": size,
        "sha256": digest,
        "stat_key": list(stat_key),
    }
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
    return marker


def _completed_upload_response(
    request: TransferRequest,
    transfer_id: str,
) -> TransferResponse:
    assert request.size is not None and request.sha256 is not None
    return TransferResponse(
        ok=True,
        action=request.action,
        transfer_id=transfer_id,
        offset=request.size,
        size=request.size,
        sha256=request.sha256,
        complete=True,
    )


def _remove_stale_completion_markers(destination: Path, *, keep: Path) -> None:
    prefix = f".{destination.name}.privy-upload-"
    for candidate in destination.parent.iterdir():
        if (
            candidate == keep
            or not candidate.name.startswith(prefix)
            or not candidate.name.endswith(".complete")
        ):
            continue
        try:
            candidate.unlink()
        except OSError as exc:
            log.warning("Could not remove stale upload marker %s: %s", candidate, exc)


def _commit_partial(
    partial: Path,
    destination: Path,
    *,
    overwrite: bool,
    location: str,
) -> None:
    try:
        if overwrite:
            os.replace(partial, destination)
            return
        os.link(partial, destination)
    except FileExistsError as exc:
        raise TransferError(
            "destination_exists",
            f"{location} destination already exists: {destination}",
        ) from exc
    except OSError as exc:
        raise TransferError(
            "filesystem_error",
            f"could not commit {location} destination {destination}: {exc}",
        ) from exc
    try:
        partial.unlink()
    except OSError as exc:
        log.warning("Committed %s but could not remove partial %s: %s", destination, partial, exc)


def _upload_verifier(
    transfer_id: str,
    stat_key: tuple[int, int, int, int],
) -> _UploadVerifier:
    with _UPLOAD_VERIFIERS_GUARD:
        verifier = _UPLOAD_VERIFIERS.get(transfer_id)
        if verifier is None or verifier.stat_key != stat_key:
            verifier = _UploadVerifier(stat_key=stat_key)
            _UPLOAD_VERIFIERS[transfer_id] = verifier
        return verifier


def _drop_upload_verifier(transfer_id: str) -> None:
    with _UPLOAD_VERIFIERS_GUARD:
        _UPLOAD_VERIFIERS.pop(transfer_id, None)


def _reap_upload_verifiers() -> None:
    cutoff = time.monotonic() - _DOWNLOAD_SESSION_RETENTION_S
    with _UPLOAD_VERIFIERS_GUARD:
        stale = [
            transfer_id
            for transfer_id, verifier in _UPLOAD_VERIFIERS.items()
            if verifier.last_used_at < cutoff
        ]
        for transfer_id in stale:
            _UPLOAD_VERIFIERS.pop(transfer_id, None)


def _reap_download_sessions() -> None:
    cutoff = time.monotonic() - _DOWNLOAD_SESSION_RETENTION_S
    with _DOWNLOAD_SESSIONS_GUARD:
        stale = [
            transfer_id
            for transfer_id, session in _DOWNLOAD_SESSIONS.items()
            if session.last_used_at < cutoff
        ]
        for transfer_id in stale:
            _DOWNLOAD_SESSIONS.pop(transfer_id, None)


def _store_download_session(transfer_id: str, session: _DownloadSession) -> None:
    with _DOWNLOAD_SESSIONS_GUARD:
        if len(_DOWNLOAD_SESSIONS) >= _MAX_DOWNLOAD_SESSIONS:
            victim = min(
                _DOWNLOAD_SESSIONS,
                key=lambda item: (
                    _DOWNLOAD_SESSIONS[item].final_digest is None,
                    _DOWNLOAD_SESSIONS[item].last_used_at,
                ),
            )
            _DOWNLOAD_SESSIONS.pop(victim, None)
        _DOWNLOAD_SESSIONS[transfer_id] = session


def _replay_download(
    session: _DownloadSession,
    request: TransferRequest,
    *,
    include_data: bool,
) -> TransferResponse:
    with session.source.open("rb") as stream:
        if _stat_key(os.fstat(stream.fileno())) != session.stat_key:
            raise TransferError("source_changed", "remote source changed during download")
        stream.seek(request.offset)
        data = stream.read(min(request.chunk_size, MAX_CHUNK_SIZE))
        if _stat_key(os.fstat(stream.fileno())) != session.stat_key:
            raise TransferError("source_changed", "remote source changed during download")
    if _stat_key(session.source.stat()) != session.stat_key:
        raise TransferError("source_changed", "remote source changed during download")
    if request.offset + len(data) != session.offset:
        raise TransferError("offset_mismatch", "download replay no longer matches session state")
    session.last_used_at = time.monotonic()
    return TransferResponse(
        ok=True,
        action=request.action,
        transfer_id=request.transfer_id,
        snapshot_id=session.snapshot_id,
        offset=session.offset,
        size=session.size,
        sha256=session.final_digest,
        data_b64=base64.b64encode(data).decode("ascii") if include_data else "",
        complete=session.offset == session.size,
    )


def _stat_key(stat: os.stat_result) -> tuple[int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _download_partial_path(destination: Path, transfer_id: str) -> Path:
    suffix = hashlib.sha256(f"{destination}\0{transfer_id}".encode()).hexdigest()[:16]
    return destination.parent / f".{destination.name}.privy-download-{suffix}.part"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.Lock())


def _request_action(raw: str | bytes) -> str:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return str(obj.get("action", "unknown"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return "unknown"
