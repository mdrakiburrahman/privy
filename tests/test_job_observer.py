import copy
import hashlib
import json
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from privy import job_observer as observer


def bindings():
    return {
        "server": "example.database.windows.net",
        "database": "privy",
        "job_id": "hubjob",
        "remote_job_id": "remotejob",
        "owner": "oldowner",
        "lease_id": "lease",
        "client_id": "client",
        "session_id": "session",
        "repo_label_location": "lease",
        "repo_label_key": "repo",
        "repo_label": "example",
        "purpose": "observing",
        "target": "dev/box_windows",
        "member_layer": "windows",
        "member": "dev/box_windows",
        "namespace": "example",
        "path": "box_windows",
        "request_sha256": hashlib.sha256(b"original").hexdigest(),
    }


def snapshot():
    b = bindings()
    return {
        "database": b["database"],
        "now": 100,
        "revision": 7,
        "record_owner": b["owner"],
        "job": {
            **{
                name: b[name]
                for name in ("job_id", "remote_job_id", "owner", "lease_id", "client_id", "target", "purpose")
            },
            "type": "exec",
            "state": "running",
            "spec": {"language": "powershell", "mode": "subprocess", "code": "original"},
        },
        "hub": {"active": False},
        "client": {"client_id": "client", "session_id": "session", "labels": {}},
        "lease": {
            "lease_id": "lease",
            "client_id": "client",
            "client_record": {"client_id": "client", "session_id": "session"},
            "state": "active",
            "expires_ts": 200,
            "purpose": "observing",
            "labels": {"repo": "example"},
            "members": {"windows": "dev/box_windows"},
            "running_jobs": ["hubjob"],
            "job_owners": {"hubjob": "oldowner"},
        },
        "targets": {"targets": [{"id": "dev/box_windows", "namespace": "example", "path": "box_windows"}]},
    }


def wire(**changes):
    result = {"job_id": "remotejob", "state": "done", "exit_code": 0, "timed_out": False, "error": None}
    result.update(changes)
    return json.dumps(result).encode()


@pytest.mark.parametrize("field", list(bindings()))
def test_missing_binding_refuses_before_dependencies(field):
    expected = bindings()
    del expected[field]
    with pytest.raises(observer.Inconclusive, match="bindings"):
        observer.validate_bindings(expected)


@pytest.mark.parametrize(
    "change",
    [
        {"exit_code": True},
        {"exit_code": "0"},
        {"exit_code": 0.0},
        {"exit_code": None},
        {"timed_out": 0},
        {"timed_out": "false"},
        {"state": "running"},
        {"state": "missing"},
        {"job_id": "other"},
        {"error": "bad_offset"},
        {"error": "unknown_job"},
        {"error": []},
        {"timed_out": True, "error": "timeout", "exit_code": -1},
    ],
)
def test_wire_invalid_or_ambiguous_is_inconclusive(change):
    with pytest.raises(observer.Inconclusive):
        observer.validate_wire(wire(**change), bindings(), "subprocess")


@pytest.mark.parametrize("field", ["exit_code", "timed_out", "job_id", "state", "error"])
def test_wire_required_fields_are_not_defaulted(field):
    raw = json.loads(wire())
    del raw[field]
    with pytest.raises(observer.Inconclusive):
        observer.validate_wire(json.dumps(raw).encode(), bindings(), "subprocess")


@pytest.mark.parametrize("exit_code", [-9, -1, 0, 1])
def test_wire_legitimate_integer_exit_is_retained_evidence(exit_code):
    assert (
        observer.validate_wire(wire(exit_code=exit_code), bindings(), "subprocess")["exit_code"] == exit_code
    )


def test_duplicate_json_fields_refused():
    with pytest.raises(observer.Inconclusive):
        observer.validate_wire(b'{"exit_code":0,"exit_code":1}', bindings(), "subprocess")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s["hub"].update(active="false"),
        lambda s: s["hub"].pop("active"),
        lambda s: s["hub"].update(active=True),
        lambda s: s["lease"].update(expires_ts=100),
        lambda s: s["lease"].update(expires_ts=True),
        lambda s: s["lease"].update(state="released"),
        lambda s: s["lease"].update(running_jobs=[]),
        lambda s: s["lease"]["job_owners"].update(hubjob="other"),
        lambda s: s["client"].update(session_id="other"),
        lambda s: s["lease"]["labels"].update(repo="other"),
        lambda s: s["targets"]["targets"][0].update(path="other"),
        lambda s: s["job"]["spec"].update(code="different"),
        lambda s: s.update(job=None),
    ],
)
def test_changed_expired_missing_or_inconsistent_fence_refuses(mutate):
    value = snapshot()
    mutate(value)
    with pytest.raises(observer.Inconclusive):
        observer.validate_snapshot(value, bindings())


def test_poll_payload_zero_ack_no_control_or_admitted_code():
    request = observer.poll_payload(snapshot()["job"], bindings())
    assert request["action"] == "poll"
    assert request["job_id"] == "remotejob"
    assert request["code"] == ""
    assert request["stdout_offset"] == request["stderr_offset"] == request["wait_s"] == 0


def test_observation_before_after_changes_never_yield_terminal_outcome():
    before = snapshot()
    after = copy.deepcopy(before)
    after["revision"] += 1
    reads = iter([before, after])
    calls = []
    result = observer.observe(
        bindings(), lambda: next(reads), lambda request: calls.append(request) or wire()
    )
    assert result["outcome"] == "inconclusive"
    assert result["reason"] == "fence_changed"
    assert len(calls) == 1


def test_observation_positive_retained_result_is_not_execution_capability():
    value = snapshot()
    result = observer.observe(bindings(), lambda: copy.deepcopy(value), lambda request: wire())
    assert result["outcome"] == "retained-terminal-result-observed"
    assert result["validated_before_and_after"] is True
    assert result["continuous_fencing"] is False
    assert result["unpin_authority"] is False


def test_observation_invalid_before_never_polls():
    value = snapshot()
    value["hub"]["active"] = True
    calls = []
    result = observer.observe(bindings(), lambda: value, lambda request: calls.append(request))
    assert result["outcome"] == "inconclusive"
    assert calls == []


def test_transport_failure_still_performs_after_fence_read():
    reads = []

    def read():
        reads.append(True)
        return snapshot()

    def failed_poll(request):
        raise observer.Inconclusive("poll_transport_failure")

    result = observer.observe(bindings(), read, failed_poll)
    assert len(reads) == 2
    assert result["reason"] == "poll_transport_failure"
    assert result["validated_before_and_after"] is True


def test_missing_worker_bindings_refuses_before_credentials_or_poll(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("must validate all bindings before dependency access")

    monkeypatch.setattr(observer, "load_settings", forbidden)
    monkeypatch.setattr(observer, "ReadOnlySql", forbidden)
    monkeypatch.setattr(observer, "poll_once", forbidden)
    result = observer.worker({"bindings": {"job_id": "incomplete"}})
    assert result["outcome"] == "inconclusive"


@pytest.mark.parametrize("status", [301, 302, 307, 308, 404, 500])
def test_transport_refuses_redirect_or_error_without_retry(status):
    calls = []
    response = SimpleNamespace(status=status, close=lambda: calls.append("response-close"))
    connection = SimpleNamespace(
        sock=None,
        request=lambda *a, **k: calls.append((a, k)),
        getresponse=lambda: response,
        close=lambda: calls.append("connection-close"),
    )
    with pytest.raises(observer.Inconclusive, match="http_status"):
        observer.poll_once(
            "https://example.servicebus.windows.net/box?secret=never-log",
            observer.poll_payload(snapshot()["job"], bindings()),
            connection_factory=lambda *a, **k: connection,
        )
    assert len([item for item in calls if isinstance(item, tuple)]) == 1


@pytest.mark.parametrize("header", [str(observer.MAX_BODY + 1), "-1", "bad"])
def test_transport_refuses_oversized_or_malformed_content_length(header):
    response = SimpleNamespace(
        status=200,
        close=lambda: None,
        getheader=lambda key, default=None: header if key == "Content-Length" else default,
    )
    connection = SimpleNamespace(
        sock=None, request=lambda *a, **k: None, getresponse=lambda: response, close=lambda: None
    )
    with pytest.raises(observer.Inconclusive, match="size_limit"):
        observer.poll_once(
            "https://example.servicebus.windows.net/box", {}, connection_factory=lambda *a, **k: connection
        )


def test_transport_bounds_response_without_content_length():
    response = SimpleNamespace(
        status=200,
        close=lambda: None,
        getheader=lambda key, default=None: default,
        read1=lambda amount: b"x" * amount,
    )
    connection = SimpleNamespace(
        sock=None, request=lambda *a, **k: None, getresponse=lambda: response, close=lambda: None
    )
    with pytest.raises(observer.Inconclusive, match="size_limit"):
        observer.poll_once(
            "https://example.servicebus.windows.net/box", {}, connection_factory=lambda *a, **k: connection
        )


def test_slow_drip_transport_interrupts_socket_by_deadline():
    reader, writer = socket.socketpair()
    stop = threading.Event()

    def drip():
        while not stop.wait(0.02):
            try:
                writer.send(b"x")
            except OSError:
                return

    thread = threading.Thread(target=drip)
    thread.start()
    response = SimpleNamespace(
        status=200,
        close=lambda: None,
        getheader=lambda key, default=None: default,
        read1=lambda amount: reader.recv(1),
    )
    connection = SimpleNamespace(
        sock=reader, request=lambda *a, **k: None, getresponse=lambda: response, close=lambda: reader.close()
    )
    started = time.monotonic()
    try:
        with pytest.raises(observer.Inconclusive):
            observer.poll_once(
                "https://example.servicebus.windows.net/box",
                {},
                seconds=0.15,
                connection_factory=lambda *a, **k: connection,
            )
        assert time.monotonic() - started < 1
    finally:
        stop.set()
        writer.close()
        reader.close()
        thread.join(1)


def test_worker_supervisor_kills_only_owned_process_on_whole_deadline(monkeypatch):
    calls = []

    class Process:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

        def communicate(self, data=None, timeout=None):
            if timeout is not None:
                raise observer.subprocess.TimeoutExpired("own-worker", timeout)
            return b"", None

        def kill(self):
            calls.append("own-worker-killed")

    monkeypatch.setattr(observer.subprocess, "Popen", lambda *a, **k: Process())
    assert observer.supervised({"bindings": bindings()})["reason"] == "whole_operation_deadline"
    assert calls == ["own-worker-killed", "closed"]


def test_real_supervisor_stops_owned_sleeping_worker_by_deadline(monkeypatch):
    popen = observer.subprocess.Popen
    processes = []

    def sleeping_worker(*args, **kwargs):
        process = popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(observer.subprocess, "Popen", sleeping_worker)
    started = time.monotonic()
    result = observer.supervised({"bindings": bindings()}, seconds=0.15)
    assert result["reason"] == "whole_operation_deadline"
    assert time.monotonic() - started < 2
    assert processes[0].poll() is not None


def test_worker_success_forbids_normal_hub_state_initialization(tmp_path, monkeypatch):
    from privy.hub.service import HubService
    from privy.hub.sql_store import DurableOutbox, SqlBackend

    def forbidden(*args, **kwargs):
        pytest.fail("normal hub initialization forbidden")

    for cls in (HubService, SqlBackend, DurableOutbox):
        monkeypatch.setattr(cls, "__init__", forbidden)
    relays = tmp_path / "authorized-test-relays.json"
    relays.write_text(
        json.dumps(
            {
                "version": 1,
                "relays": [
                    {
                        "name": "dev",
                        "namespace": "example",
                        "keyrule": "test",
                        "key": "dummy",
                        "hybrid_connections": ["box_windows"],
                    }
                ],
            }
        )
    )
    closed = []
    calls = []
    monkeypatch.setattr(observer, "load_settings", lambda *args: object())
    settings = tmp_path / "test-settings.yaml"
    settings.write_text("test-only")
    monkeypatch.setattr(
        observer,
        "ReadOnlySql",
        lambda *args: SimpleNamespace(
            snapshot=snapshot,
            close=lambda: closed.append(True),
        ),
    )
    monkeypatch.setattr(observer, "poll_once", lambda url, request: calls.append(request) or wire())
    result = observer.worker({"bindings": bindings(), "settings": str(settings), "relays": str(relays)})
    assert result["outcome"] == "retained-terminal-result-observed"
    assert len(calls) == 1 and calls[0]["code"] == ""
    assert closed == [True]


def test_sql_snapshots_are_exact_key_serializable_rollback_close_only(monkeypatch):
    calls = []
    value = snapshot()
    results = iter(
        [
            [(value["database"], value["now"])],
            [(value["record_owner"], value["revision"], json.dumps(value["job"]))],
            [
                ("hub", "oldowner", json.dumps(value["hub"])),
                ("lease", "lease", json.dumps(value["lease"])),
                ("client", "client", json.dumps(value["client"])),
                ("config", "targets", json.dumps(value["targets"])),
            ],
        ]
    )

    class Cursor:
        rows = None

        def execute(self, query, *args):
            calls.append((query, args))
            if query.startswith("SELECT"):
                self.rows = next(results)

        def fetchone(self):
            return self.rows[0]

        def fetchall(self):
            return self.rows

        def close(self):
            calls.append("cursor-close")

    connection = SimpleNamespace(
        cursor=Cursor,
        rollback=lambda: calls.append("rollback"),
        close=lambda: calls.append("connection-close"),
    )
    credential = SimpleNamespace(
        get_token=lambda *a: SimpleNamespace(token="not-logged", expires_on=time.time() + 1000),
        close=lambda: calls.append("credential-close"),
    )
    monkeypatch.setitem(sys.modules, "pyodbc", SimpleNamespace(connect=lambda *a, **k: connection))
    monkeypatch.setitem(
        sys.modules, "azure.identity", SimpleNamespace(AzureCliCredential=lambda **k: credential)
    )
    sql = observer.ReadOnlySql(
        SimpleNamespace(server=bindings()["server"], database="privy", driver="test"), bindings()
    )
    try:
        assert sql.snapshot() == value
    finally:
        sql.close()
    queries = [item for item in calls if isinstance(item, tuple)]
    assert "SERIALIZABLE" in queries[0][0] and "LOCK_TIMEOUT 2000" in queries[0][0]
    assert queries[2][1][-1] == "hubjob"
    assert queries[3][1][-3:] == ("oldowner", "lease", "client")
    assert all(query.startswith(("SELECT", "SET")) for query, _ in queries)
    assert calls[-4:] == ["cursor-close", "rollback", "connection-close", "credential-close"]


def test_cli_missing_arguments_never_initializes_hub(monkeypatch, capsys):
    from privy.cli import main
    from privy.hub.service import HubService
    from privy.hub.sql_store import DurableOutbox, SqlBackend

    def forbidden(*args, **kwargs):
        pytest.fail("observer must never initialize normal hub state")

    monkeypatch.setattr(SqlBackend, "__init__", forbidden)
    monkeypatch.setattr(DurableOutbox, "__init__", forbidden)
    monkeypatch.setattr(HubService, "__init__", forbidden)
    assert main(["observe-job"]) == 2
    assert "no auto-discovery" in capsys.readouterr().err


@pytest.mark.parametrize(
    "streaming,trimmed,undrained",
    [(False, False, False), (True, False, False), (True, True, False), (True, False, True)],
)
def test_real_retained_poll_zero_ack_annotation_and_inconclusive_buffers(
    monkeypatch, streaming, trimmed, undrained
):
    from privy import executor
    from privy.protocol import ExecRequest, ExecResponse

    request = ExecRequest(kind="powershell", code="not-executed", stream=streaming)
    job = executor._Job(request)
    job.id = "remotejob"
    job.response = ExecResponse.from_output(exit_code=0, stdout=b"", stderr=b"", duration_ms=1)
    job.done = None
    job.finished_at = time.monotonic()
    if streaming:
        job.out.append(b"x" * (executor.MAX_STREAM_CHUNK_BYTES if undrained else 4))
        if trimmed:
            job.out.trim(1)
    monkeypatch.setattr(executor, "_JOBS", {job.id: job})
    before_finished = job.finished_at
    before_base = job.out._base
    before_response = job.response.to_json()
    payload = observer.poll_payload(snapshot()["job"], bindings())
    reply = executor.poll_job(
        payload["job_id"], payload["wait_s"], payload["stdout_offset"], payload["stderr_offset"]
    )
    assert job.out._base == before_base
    assert job.finished_at == before_finished and executor._JOBS[job.id] is job
    if streaming:
        assert job.response.to_json() == before_response
    else:
        assert job.response.job_id == "remotejob" and job.response.state == "done"
    if trimmed or undrained:
        with pytest.raises(observer.Inconclusive):
            observer.validate_wire(reply.to_json().encode(), bindings(), "subprocess")
    else:
        assert observer.validate_wire(reply.to_json().encode(), bindings(), "subprocess")["exit_code"] == 0
