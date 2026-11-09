"""One-shot retained-result observation; never initializes hub persistence or control."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from privy import __version__
from privy._relay import create_http_send_url, create_sas_token
from privy.hub.config import parse_relays_document
from privy.hub.settings import load_settings

MAX_BODY = 512 * 1024
MAX_FILE = 1024 * 1024
OPERATION_SECONDS = 30
POLL_SECONDS = 8
BINDINGS = (
    "server",
    "database",
    "job_id",
    "remote_job_id",
    "owner",
    "lease_id",
    "client_id",
    "session_id",
    "repo_label_location",
    "repo_label_key",
    "repo_label",
    "purpose",
    "target",
    "member_layer",
    "member",
    "namespace",
    "path",
    "request_sha256",
)


class Inconclusive(ValueError):
    """Sanitized reason code, never a credential-bearing exception."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Inconclusive("duplicate_json_field")
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Inconclusive("invalid_json") from exc


def validate_bindings(expected):
    if not isinstance(expected, dict) or set(expected) != set(BINDINGS):
        raise Inconclusive("missing_or_unknown_bindings")
    if any(
        not isinstance(value, str) or not value.strip() or len(value) > 1024 for value in expected.values()
    ):
        raise Inconclusive("invalid_bindings")
    if expected["repo_label_location"] not in ("lease", "client"):
        raise Inconclusive("invalid_repo_label_location")
    if not re.fullmatch(r"[0-9a-f]{64}", expected["request_sha256"]):
        raise Inconclusive("invalid_request_hash")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", expected["server"]):
        raise Inconclusive("invalid_sql_server")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", expected["database"]):
        raise Inconclusive("invalid_database")
    return dict(expected)


def validate_snapshot(value, expected):
    try:
        job, lease, client = value["job"], value["lease"], value["client"]
        if value["database"] != expected["database"] or value["hub"]["active"] is not False:
            raise Inconclusive("owner_not_explicitly_inactive")
        if (
            type(value["revision"]) is not int
            or value["revision"] < 0
            or value["record_owner"] != expected["owner"]
        ):
            raise Inconclusive("job_owner_revision_mismatch")
        if any(
            job[name] != expected[name]
            for name in (
                "job_id",
                "remote_job_id",
                "owner",
                "lease_id",
                "client_id",
                "target",
                "purpose",
            )
        ):
            raise Inconclusive("job_binding_mismatch")
        spec = job["spec"]
        if job["type"] != "exec" or job["state"] != "running":
            raise Inconclusive("not_running_exec_record")
        if spec["language"] not in ("bash", "powershell", "python") or spec["mode"] not in (
            "subprocess",
            "inprocess",
        ):
            raise Inconclusive("invalid_admitted_request")
        if spec["mode"] == "inprocess" and spec["language"] != "python":
            raise Inconclusive("invalid_admitted_request")
        if hashlib.sha256(spec["code"].encode("utf-8")).hexdigest() != expected["request_sha256"]:
            raise Inconclusive("request_hash_mismatch")
        if client["client_id"] != expected["client_id"] or client["session_id"] != expected["session_id"]:
            raise Inconclusive("client_session_mismatch")
        if lease["client_record"]["session_id"] != expected["session_id"]:
            raise Inconclusive("holder_session_mismatch")
        if lease["client_record"]["client_id"] != expected["client_id"]:
            raise Inconclusive("holder_client_mismatch")
        if any(lease[name] != expected[name] for name in ("lease_id", "client_id", "purpose")):
            raise Inconclusive("lease_binding_mismatch")
        now, expiry = value["now"], lease["expires_ts"]
        if any(type(n) not in (int, float) or not math.isfinite(n) for n in (now, expiry)):
            raise Inconclusive("invalid_database_expiry")
        if lease["state"] != "active" or expiry <= now:
            raise Inconclusive("lease_not_active_unexpired")
        if not isinstance(lease["running_jobs"], list) or not isinstance(lease["job_owners"], dict):
            raise Inconclusive("malformed_pin")
        if (
            expected["job_id"] not in lease["running_jobs"]
            or lease["job_owners"][expected["job_id"]] != expected["owner"]
        ):
            raise Inconclusive("matching_pin_missing")
        if lease["members"][expected["member_layer"]] != expected["member"]:
            raise Inconclusive("member_mismatch")
        location = lease if expected["repo_label_location"] == "lease" else client
        if location["labels"][expected["repo_label_key"]] != expected["repo_label"]:
            raise Inconclusive("repo_label_mismatch")
        mappings = [t for t in value["targets"]["targets"] if t["id"] == expected["target"]]
        if mappings != [
            {"id": expected["target"], "namespace": expected["namespace"], "path": expected["path"]}
        ]:
            raise Inconclusive("target_mapping_mismatch")
        if (
            expected["target"].partition("/")[2] != expected["path"]
            or expected["target"] != expected["member"]
        ):
            raise Inconclusive("canonical_target_mismatch")
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError) as exc:
        if isinstance(exc, Inconclusive):
            raise
        raise Inconclusive("missing_or_malformed_fence") from exc
    return job


def poll_payload(job, expected):
    spec = job["spec"]
    return {
        "kind": spec["language"],
        "mode": spec["mode"],
        "code": "",
        "timeout_s": 1,
        "action": "poll",
        "job_id": expected["remote_job_id"],
        "wait_s": 0,
        "stream": False,
        "stdout_offset": 0,
        "stderr_offset": 0,
        "protocol_version": 1,
    }


def validate_wire(raw, expected, mode):
    if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
        raise Inconclusive("response_size_limit")
    obj = _json(raw)
    if not isinstance(obj, dict) or not {"job_id", "state", "exit_code", "timed_out", "error"} <= obj.keys():
        raise Inconclusive("missing_wire_fields")
    if type(obj["exit_code"]) is not int or type(obj["timed_out"]) is not bool:
        raise Inconclusive("invalid_wire_types")
    if obj["job_id"] != expected["remote_job_id"] or obj["state"] not in ("done", "cancelled"):
        raise Inconclusive("nonterminal_or_wrong_remote_id")
    if obj["error"] is not None and (
        type(obj["error"]) is not str or obj["error"] not in ("timeout", "cancelled")
    ):
        raise Inconclusive("nonexecution_protocol_error")
    if (
        mode == "subprocess"
        and obj["timed_out"] is True
        and obj["error"] == "timeout"
        and obj["exit_code"] == -1
    ):
        raise Inconclusive("unobserved_subprocess_timeout")
    return {name: obj[name] for name in ("job_id", "state", "exit_code", "timed_out", "error")}


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps({k: v for k, v in value.items() if k != "now"}, sort_keys=True).encode()
    ).hexdigest()


def observe(expected, read_snapshot, poll):
    receipt = {
        "outcome": "inconclusive",
        "version": __version__,
        "started_at": _utc(),
        "continuous_fencing": False,
        "unpin_authority": False,
        "validated_before_and_after": False,
        "poll_attempted": False,
        "legacy_retained_response_annotations": ["job.response.job_id", "job.response.state"],
        "descendant_exit_proof": False,
        "up_permission": False,
    }
    try:
        expected = validate_bindings(expected)
        receipt["bindings"] = expected
        before = read_snapshot()
        job = validate_snapshot(before, expected)
        receipt["before_database_time"] = before["now"]
        receipt["job_revision"] = before["revision"]
        receipt["before_checked_at"] = _utc()
        receipt["poll_attempted"] = True
        poll_error = None
        try:
            raw = poll(poll_payload(job, expected))
        except Inconclusive as exc:
            raw = b""
            poll_error = exc
        receipt["listener_received_at"] = _utc()
        after = read_snapshot()
        validate_snapshot(after, expected)
        receipt["after_database_time"] = after["now"]
        receipt["after_checked_at"] = _utc()
        if _fingerprint(before) != _fingerprint(after):
            raise Inconclusive("fence_changed")
        receipt["validated_before_and_after"] = True
        if poll_error is not None:
            raise poll_error
        receipt["evidence"] = validate_wire(raw, expected, job["spec"]["mode"])
        receipt["outcome"] = "retained-terminal-result-observed"
        receipt["reason"] = "retained_result_only_not_process_tree_exit_or_up_permission"
    except Inconclusive as exc:
        receipt["reason"] = str(exc)
    finally:
        receipt["finished_at"] = _utc()
    return receipt


class ReadOnlySql:
    """Fresh short SERIALIZABLE read transactions; no backend, outbox or incarnation."""

    def __init__(self, settings, expected):
        import pyodbc
        from azure.identity import AzureCliCredential

        if settings.server != expected["server"] or settings.database != expected["database"]:
            raise Inconclusive("settings_database_mismatch")
        pyodbc.pooling = False  # This object lives only in the deadline-supervised worker process.
        self.odbc = pyodbc
        self.settings = settings
        self.expected = expected
        self.credential = AzureCliCredential(process_timeout=5)
        self.token = None

    def close(self):
        self.token = None
        self.credential.close()

    @contextmanager
    def _read(self):
        if self.token is None or self.token.expires_on <= time.time() + 60:
            self.token = self.credential.get_token("https://database.windows.net/.default")
        raw = self.token.token.encode("utf-16-le")
        s = self.settings
        connection = self.odbc.connect(
            f"Driver={{{s.driver}}};Server=tcp:{s.server},1433;Database={s.database};"
            "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=3;",
            attrs_before={1256: struct.pack("<I", len(raw)) + raw},
            autocommit=False,
        )
        try:
            connection.timeout = 3
            cursor = connection.cursor()
            try:
                cursor.execute("SET LOCK_TIMEOUT 2000; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;")
                yield cursor
            finally:
                cursor.close()
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

    def snapshot(self):
        b = self.expected
        with self._read() as cursor:
            cursor.execute("SELECT DB_NAME(),DATEDIFF_BIG(millisecond,'1970-01-01',SYSUTCDATETIME())/1000.0")
            database, now = cursor.fetchone()
            cursor.execute(
                "SELECT owner,revision,CASE WHEN DATALENGTH(payload)<=? THEN payload END "
                "FROM dbo.privy_records WHERE kind='job' AND id=?",
                MAX_FILE,
                b["job_id"],
            )
            job = cursor.fetchone()
            if job is None or job[2] is None:
                raise Inconclusive("missing_or_oversized_job")
            cursor.execute(
                "SELECT kind,id,CASE WHEN DATALENGTH(payload)<=? THEN payload END "
                "FROM dbo.privy_coordination WHERE (kind='hub' AND id=?) OR "
                "(kind='lease' AND id=?) OR (kind='client' AND id=?) OR (kind='config' AND id='targets')",
                MAX_FILE,
                b["owner"],
                b["lease_id"],
                b["client_id"],
            )
            rows = cursor.fetchall()
            if len(rows) != 4 or any(row[2] is None for row in rows):
                raise Inconclusive("missing_or_oversized_coordination")
            records = {(kind, key): _json(payload) for kind, key, payload in rows}
            return {
                "database": database,
                "now": float(now),
                "revision": job[1],
                "record_owner": job[0],
                "job": _json(job[2]),
                "hub": records["hub", b["owner"]],
                "lease": records["lease", b["lease_id"]],
                "client": records["client", b["client_id"]],
                "targets": records["config", "targets"],
            }


def poll_once(url, request, *, seconds=POLL_SECONDS, connection_factory=http.client.HTTPSConnection):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".servicebus.windows.net")
    ):
        raise Inconclusive("invalid_relay_endpoint")
    if parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise Inconclusive("invalid_relay_endpoint")
    connection = connection_factory(parsed.hostname, timeout=min(2, seconds))
    deadline = time.monotonic() + seconds
    sock = None

    def interrupt():
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                sock.close()
        connection.close()

    timer = threading.Timer(seconds, interrupt)
    timer.daemon = True
    timer.start()
    try:
        connection.request(
            "POST",
            parsed.path + ("?" + parsed.query if parsed.query else ""),
            body=json.dumps(request).encode(),
            headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
        )
        sock = connection.sock
        response = connection.getresponse()
        try:
            if response.status != 200:
                raise Inconclusive("relay_http_status")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise Inconclusive("encoded_response_refused")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_BODY):
                raise Inconclusive("response_size_limit")
            body = bytearray()
            while len(body) <= MAX_BODY:
                if time.monotonic() >= deadline:
                    raise Inconclusive("poll_deadline")
                chunk = response.read1(min(8192, MAX_BODY + 1 - len(body)))
                if time.monotonic() >= deadline:
                    raise Inconclusive("poll_deadline")
                if not chunk:
                    return bytes(body)
                body.extend(chunk)
            raise Inconclusive("response_size_limit")
        finally:
            response.close()
    except (OSError, http.client.HTTPException) as exc:
        raise Inconclusive("poll_transport_failure") from exc
    finally:
        timer.cancel()
        connection.close()
        timer.join(timeout=0.1)


def _file(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise Inconclusive("local_input_size_limit")
    return raw


def load_bindings(path):
    try:
        expected = yaml.safe_load(_file(path))
    except (OSError, yaml.YAMLError) as exc:
        raise Inconclusive("unreadable_bindings") from exc
    return validate_bindings(expected)


def worker(document):
    sql = None
    try:
        expected = (
            validate_bindings(document["bindings"])
            if "bindings" in document
            else load_bindings(document["bindings_file"])
        )
        _file(document["settings"])
        settings = load_settings(Path.cwd(), Path(document["settings"]))
        if settings is None:
            raise Inconclusive("explicit_sql_settings_required")
        relays = parse_relays_document(_json(_file(document["relays"])))
        targets = [t for r in relays for t in r.targets if t.id == expected["target"]]
        if (
            len(targets) != 1
            or targets[0].namespace != expected["namespace"]
            or targets[0].path != expected["path"]
        ):
            raise Inconclusive("local_relay_mapping_mismatch")
        target = targets[0]
        token = target.token or create_sas_token(
            target.namespace, target.path, target.keyrule, target.key, ttl_seconds=60
        )
        url = create_http_send_url(target.namespace, target.path, token)
        sql = ReadOnlySql(settings, expected)

        def read_snapshot():
            try:
                return sql.snapshot()
            except Inconclusive:
                raise
            except Exception as exc:
                raise Inconclusive("sql_read_failure") from exc

        return observe(expected, read_snapshot, lambda request: poll_once(url, request))
    except Exception:  # Boundary reports sanitized failure, never SQL/Relay exception text.
        return {"outcome": "inconclusive", "reason": "observer_initialization_or_read_failure"}
    finally:
        if sql is not None:
            sql.close()


def supervised(document, *, seconds=OPERATION_SECONDS):
    """A private worker process bounds DNS, credential providers and native ODBC calls too."""
    if "bindings" in document:
        validate_bindings(document["bindings"])
    elif not isinstance(document.get("bindings_file"), str) or not document["bindings_file"]:
        raise Inconclusive("missing_bindings_file")
    command = (
        [sys.executable, "observe-job", "--worker"]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "privy.job_observer"]
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment
    ) as process:
        try:
            output, _ = process.communicate(json.dumps(document).encode(), timeout=seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return {"outcome": "inconclusive", "reason": "whole_operation_deadline"}
        if process.returncode != 0 or len(output) > MAX_FILE:
            return {"outcome": "inconclusive", "reason": "worker_failure"}
        result = _json(output)
        if not isinstance(result, dict) or result.get("outcome") not in (
            "inconclusive",
            "retained-terminal-result-observed",
        ):
            raise Inconclusive("invalid_worker_receipt")
        return result


def worker_main():
    timer = threading.Timer(OPERATION_SECONDS, lambda: os._exit(124))
    timer.daemon = True
    timer.start()
    try:
        document = _json(sys.stdin.buffer.read(MAX_FILE + 1))
        result = worker(document)
        path = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)
        with path.open("rb") as stream:
            result["artifact_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        result["artifact_kind"] = (
            "packaged-executable" if getattr(sys, "frozen", False) else "observer-source-module"
        )
        print(json.dumps(result, allow_nan=False))
    finally:
        timer.cancel()


if __name__ == "__main__":
    worker_main()
