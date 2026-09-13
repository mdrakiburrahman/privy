"""Opt-in, private terminal artifacts for the native CLI batch scheduler."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from privy.batch import BatchManifest, BatchResult, CommandOutcome

RESULTS_VERSION = 1
log = logging.getLogger("privy.batch_results")


class BatchResultsError(OSError):
    """A local batch artifact could not be safely published."""


class BatchResultsWriter:
    """Write immutable JSON files in a new, descriptor-anchored directory."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        manifest: BatchManifest,
        *,
        source: str,
        raw_manifest: str | bytes,
    ) -> None:
        self.batch_id = uuid.uuid4().hex
        self._commands = {
            command.id: (index, f"command-{index + 1:06d}.json")
            for index, command in enumerate(manifest.commands)
        }
        self._written: set[str] = set()
        self._parent_fd, self._fd, self._name = _open_fresh_directory(path)
        try:
            self._identity = os.fstat(self._fd)
            raw = raw_manifest.encode("utf-8") if isinstance(raw_manifest, str) else raw_manifest
            self._publish(
                "batch.json",
                {
                    "schema": "privy.batch.results",
                    "version": RESULTS_VERSION,
                    "batch_id": self.batch_id,
                    "manifest": {
                        "source": source,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "max_parallel": manifest.max_parallel,
                    },
                    "commands": [
                        {"index": index, "id": command_id, "file": filename}
                        for command_id, (index, filename) in self._commands.items()
                    ],
                },
            )
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> BatchResultsWriter:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self._fd is not None:
                os.close(self._fd)
        finally:
            self._fd = None
            if self._parent_fd is not None:
                os.close(self._parent_fd)
                self._parent_fd = None

    def write_outcome(self, outcome: CommandOutcome) -> None:
        if outcome.id not in self._commands:
            raise BatchResultsError(f"unknown terminal command id: {outcome.id!r}")
        if outcome.id in self._written:
            raise BatchResultsError(f"terminal command already published: {outcome.id!r}")
        if outcome.state not in ("succeeded", "failed", "skipped", "cancelled"):
            raise BatchResultsError(f"command {outcome.id!r} is not terminal: {outcome.state!r}")
        index, filename = self._commands[outcome.id]
        self._publish(
            filename,
            {
                "schema": "privy.batch.command",
                "version": RESULTS_VERSION,
                "batch_id": self.batch_id,
                "index": index,
                "outcome": outcome.to_dict(),
            },
        )
        self._written.add(outcome.id)

    def complete(
        self,
        result: BatchResult | None,
        *,
        errors: Iterable[tuple[str | None, str]] = (),
        interrupted: bool = False,
    ) -> bool:
        failures = [{"command_id": command_id, "error": error} for command_id, error in errors]
        missing = [command_id for command_id in self._commands if command_id not in self._written]
        if missing:
            failures.append({"command_id": None, "error": "missing terminal command artifacts"})
        if result is None and not failures:
            failures.append({"command_id": None, "error": "batch did not return a native result"})
        state = "interrupted" if interrupted else "error" if failures else "complete"
        self._publish(
            "complete.json",
            {
                "schema": "privy.batch.complete",
                "version": RESULTS_VERSION,
                "batch_id": self.batch_id,
                "state": state,
                "command_count": len(self._commands),
                "artifact_count": len(self._written),
                "missing_command_ids": missing,
                "errors": failures,
                "result": result.to_dict() if result is not None else None,
            },
        )
        return state == "complete"

    def _check_directory(self) -> None:
        if self._fd is None:
            raise BatchResultsError("batch results directory is closed")
        current = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
        if (
            (current.st_dev, current.st_ino) != (self._identity.st_dev, self._identity.st_ino)
            or not stat.S_ISDIR(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o700
        ):
            raise BatchResultsError("batch results directory was replaced or its permissions changed")

    def _publish(self, filename: str, value: dict[str, Any]) -> None:
        # Serialize before creating even the staging file. A hard link publishes
        # the fully synced inode atomically without replacing an existing name.
        staging = f".pending-{uuid.uuid4().hex}"
        try:
            encoded = (json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
            self._check_directory()
            fd = os.open(
                staging,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._fd,
            )
            primary_error = None
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(fd)
                self._check_directory()
                os.link(
                    staging,
                    filename,
                    src_dir_fd=self._fd,
                    dst_dir_fd=self._fd,
                    follow_symlinks=False,
                )
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                self._discard_staging(fd, staging, primary_error)
            os.fsync(self._fd)
        except (OSError, TypeError, ValueError, NotImplementedError) as exc:
            raise BatchResultsError(f"cannot publish batch artifact {filename!r}: {exc}") from exc

    def _discard_staging(self, fd: int, staging: str, primary_error: BaseException | None) -> None:
        errors = []
        try:
            os.close(fd)
        except OSError as exc:
            errors.append(exc)
        try:
            os.unlink(staging, dir_fd=self._fd)
        except OSError as exc:
            errors.append(exc)
        if errors:
            for error in errors:
                log.error("Batch artifact staging cleanup failed: %s", error)
            # In particular, an unlink error must not replace KeyboardInterrupt
            # and turn native cancellation into ordinary callback-error draining.
            if primary_error is None:
                raise errors[0]


def _open_fresh_directory(path: str | os.PathLike[str]) -> tuple[int, int, str]:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise BatchResultsError("--results-dir requires POSIX private, no-follow directory operations")
    directory = Path(path)
    parts = directory.parts[1:] if directory.is_absolute() else directory.parts
    if not parts or ".." in parts:
        raise BatchResultsError("--results-dir must name a new directory without '..' components")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = None
    fd = None
    try:
        parent_fd = os.open(directory.anchor or ".", flags)
        for component in parts[:-1]:
            child_fd = os.open(component, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        name = parts[-1]
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        fd = os.open(name, flags, dir_fd=parent_fd)
        os.fchmod(fd, 0o700)
        os.fsync(parent_fd)
        return parent_fd, fd, name
    except (OSError, ValueError, NotImplementedError) as exc:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)
        raise BatchResultsError(f"cannot create fresh --results-dir {str(path)!r}: {exc}") from exc
