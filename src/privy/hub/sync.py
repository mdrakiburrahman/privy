"""Non-destructive sync using existing listener Python execution and verified uploads."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from privy.transfer import DEFAULT_CHUNK_SIZE, TransferError

MAX_SYNC_ENTRIES = 10_000
MAX_SYNC_MANIFEST_BYTES = 1024 * 1024
Report = Callable[[dict[str, Any]], None]


def _link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def validate_source(local_path: str, destination_type: str) -> Path:
    """Validate the cheap request-boundary properties before starting a worker."""
    if destination_type not in ("directory", "file"):
        raise TransferError("invalid_argument", "destination_type must be directory or file")
    source = Path(local_path).expanduser()
    if not local_path.strip() or not source.is_absolute():
        raise TransferError("invalid_argument", "local_path must be an absolute path on the hub")
    try:
        info = source.lstat()
    except OSError as exc:
        raise TransferError("local_source_unavailable", f"cannot read local source: {exc}") from exc
    if _link(info):
        raise TransferError("unsupported_link", "local source must not be a link or junction")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise TransferError("unsupported_file", "local source must be a regular file or directory")
    if stat.S_ISDIR(info.st_mode) and destination_type == "file":
        raise TransferError("invalid_argument", "a source directory needs destination_type=directory")
    return source


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@dataclass
class _Entry:
    source: Path
    path: str
    directory: bool
    signature: tuple[int, int, int, int]
    size: int = 0
    sha256: str = ""

    def document(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "directory": self.directory,
            "size": self.size,
            "sha256": self.sha256,
        }


def _scan(source: Path, destination_type: str, check_cancel: Callable[[], None]) -> list[_Entry]:
    root_info = source.lstat()
    name = "." if stat.S_ISDIR(root_info.st_mode) or destination_type == "file" else source.name
    pending = [(source, name)]
    entries = []
    serialized_size = 2
    while pending:
        check_cancel()
        path, relative = pending.pop()
        info = path.lstat()
        if _link(info):
            raise TransferError("unsupported_link", f"source contains a link or junction: {path}")
        directory = stat.S_ISDIR(info.st_mode)
        if not directory and not stat.S_ISREG(info.st_mode):
            raise TransferError("unsupported_file", f"source contains a special file: {path}")
        entry = _Entry(path, relative, directory, _signature(info))
        if not directory:
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                while True:
                    check_cancel()
                    chunk = stream.read(DEFAULT_CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
            if _signature(path.lstat()) != entry.signature:
                raise TransferError("source_changed", f"source changed during hashing: {path}")
            entry.size, entry.sha256 = info.st_size, hasher.hexdigest()
        serialized_size += len(json.dumps(entry.document(), ensure_ascii=True).encode("utf-8"))
        serialized_size += 2 if entries else 0
        if len(entries) >= MAX_SYNC_ENTRIES or serialized_size > MAX_SYNC_MANIFEST_BYTES:
            raise TransferError(
                "sync_limit_exceeded", "sync manifest exceeds the entry or serialized byte limit"
            )
        entries.append(entry)
        if directory:
            with os.scandir(path) as children:
                for child in children:
                    check_cancel()
                    if len(entries) + len(pending) >= MAX_SYNC_ENTRIES:
                        raise TransferError("sync_limit_exceeded", "sync manifest exceeds the entry limit")
                    if "\\" in child.name:
                        raise TransferError("invalid_path", f"source name contains a backslash: {child.name}")
                    child_relative = child.name if relative == "." else relative + "/" + child.name
                    pending.append((Path(child.path), child_relative))
    return entries


_REMOTE_PREPARE = r"""
import hashlib
import json
import os
import stat
from pathlib import Path
from privy import transfer

def no_links(path):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("destination contains a link or junction: " + str(item))

def prepare(document):
    root = Path(document["remote_path"]).expanduser()
    if not root.is_absolute():
        raise ValueError("remote_path must be absolute on the target")
    root = Path(os.path.abspath(root))
    no_links(root)
    entries = document["entries"]
    destinations = []
    seen = set()
    for entry in entries:
        relative = entry["path"]
        parts = relative.split("/")
        if relative != ".":
            for part in parts:
                if not part or part in (".", "..") or "\\" in part or any(ord(c) < 32 for c in part):
                    raise ValueError("invalid relative source name")
                if os.name == "nt":
                    reserved = part.split(".", 1)[0].upper()
                    if (any(c in part for c in ':<>|"*?') or part.endswith((".", " "))
                            or reserved in {"CON", "PRN", "AUX", "NUL"}
                            or reserved in {"COM" + str(i) for i in range(10)}
                            or reserved in {"LPT" + str(i) for i in range(10)}):
                        raise ValueError("invalid Windows destination name: " + part)
        destination = root if relative == "." else root.joinpath(*parts)
        identity = os.path.normcase(str(destination))
        if identity in seen:
            raise ValueError("source names collide on the destination: " + relative)
        seen.add(identity)
        no_links(destination)
        if destination.exists():
            directory = entry["directory"]
            if (directory and not destination.is_dir()) or (not directory and not destination.is_file()):
                raise ValueError("destination type conflict: " + str(destination))
        destinations.append(destination)
    same = []
    for index, (entry, destination) in enumerate(zip(entries, destinations)):
        if entry["directory"] or not destination.exists():
            continue
        before = destination.stat()
        if before.st_size == entry["size"]:
            hasher = hashlib.sha256()
            with destination.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            after = destination.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError("destination changed during comparison: " + str(destination))
            if hasher.hexdigest() == entry["sha256"]:
                migrate = getattr(transfer, "_migrate_upload_receipt", None)
                if migrate is not None:
                    migrate(destination, entry["size"], entry["sha256"])
                same.append(index)
                continue
        if not document["overwrite"]:
            raise ValueError("destination exists; overwrite=false: " + str(destination))
    for entry, destination in zip(entries, destinations):
        no_links(destination)
        (destination if entry["directory"] else destination.parent).mkdir(parents=True, exist_ok=True)
    return {"root": str(root), "windows": os.name == "nt", "same": same}

try:
    print(json.dumps(prepare(document), ensure_ascii=True))
except (OSError, ValueError) as exc:
    print(json.dumps({"error": str(exc)}, ensure_ascii=True))
"""


def _prepare(
    entries: list[_Entry], remote_path: str, overwrite: bool, run_remote: Callable[[str], str]
) -> tuple[PureWindowsPath | PurePosixPath, set[int]]:
    document = {
        "remote_path": remote_path,
        "overwrite": overwrite,
        "entries": [e.document() for e in entries],
    }
    raw = run_remote(f"document = {document!r}\n" + _REMOTE_PREPARE)
    try:
        result = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise TransferError("sync_bad_response", "invalid sync helper response: expected JSON") from exc
    if isinstance(result, dict) and isinstance(result.get("error"), str):
        raise TransferError("sync_destination_error", result["error"])
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("root"), str)
        or not isinstance(result.get("windows"), bool)
        or not isinstance(result.get("same"), list)
        or any(
            type(index) is not int or not 0 <= index < len(entries) or entries[index].directory
            for index in result["same"]
        )
        or len(set(result["same"])) != len(result["same"])
    ):
        raise TransferError("sync_bad_response", "invalid sync helper response metadata")
    root = PureWindowsPath(result["root"]) if result["windows"] else PurePosixPath(result["root"])
    if not root.is_absolute():
        raise TransferError("sync_bad_response", "sync helper response root must be absolute")
    return root, set(result["same"])


def sync(
    client: Any,
    local_path: str,
    remote_path: str,
    *,
    destination_type: str,
    overwrite: bool,
    check_cancel: Callable[[], None],
    report: Report,
    run_remote: Callable[[str], str],
) -> dict[str, Any]:
    """Copy one bounded source tree; failures leave already completed files intact."""
    counts: dict[str, Any] = {
        "phase": "scanning",
        "files_total": 0,
        "files_done": 0,
        "files_copied": 0,
        "files_skipped": 0,
        "directories_total": 0,
        "bytes_total": 0,
        "bytes_done": 0,
        "bytes_copied": 0,
        "current_path": None,
    }
    report(dict(counts))
    try:
        source = validate_source(local_path, destination_type)
        entries = _scan(source, destination_type, check_cancel)
        counts.update(
            files_total=sum(not entry.directory for entry in entries),
            directories_total=sum(entry.directory for entry in entries),
            bytes_total=sum(entry.size for entry in entries),
            phase="preparing",
        )
        report(dict(counts))
        check_cancel()
        root, same = _prepare(entries, remote_path, overwrite, run_remote)
        check_cancel()
        counts["phase"] = "uploading"
        for index, entry in enumerate(entries):
            check_cancel()
            if entry.directory:
                continue
            counts["current_path"] = entry.path
            info = entry.source.lstat()
            if _link(info) or _signature(info) != entry.signature:
                raise TransferError("source_changed", f"source changed before upload: {entry.source}")
            if index in same:
                counts["files_skipped"] += 1
            else:
                start = counts["bytes_done"]

                def progress(done: int, total: int, *, start: int = start) -> None:
                    check_cancel()
                    update = {**counts, "bytes_done": start + done}
                    report(update)

                destination = root if entry.path == "." else root.joinpath(*entry.path.split("/"))
                result = client.upload_file(
                    str(entry.source), str(destination), overwrite=overwrite, progress=progress
                )
                if result.size != entry.size or result.sha256 != entry.sha256:
                    raise TransferError("source_changed", f"source changed during upload: {entry.source}")
                counts["files_copied"] += 1
                counts["bytes_copied"] += entry.size
            counts["files_done"] += 1
            counts["bytes_done"] += entry.size
            report(dict(counts))
        check_cancel()
        counts.update(phase="completed", current_path=None)
        report(dict(counts))
        return counts
    except OSError as exc:
        raise TransferError("sync_filesystem_error", str(exc)) from exc
