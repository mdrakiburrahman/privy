import copy
import hashlib
import io
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
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
            if timeout != 2:
                raise observer.subprocess.TimeoutExpired("own-worker", timeout)
            return b"", None

        def close_tree(self):
            calls.append("own-worker-killed")

    monkeypatch.setattr(observer, "require_external_launcher", lambda: None)
    monkeypatch.setattr(observer, "OwnedProcess", lambda *a, **k: Process())
    assert observer.supervised({"bindings": bindings()})["reason"] == "whole_operation_deadline"
    assert calls == ["own-worker-killed", "closed"]


@pytest.mark.skipif(os.name != "nt", reason="Windows owned Job Object containment")
def test_real_supervisor_stops_owned_sleeping_worker_by_deadline(monkeypatch):
    popen = observer.OwnedProcess
    processes = []

    def sleeping_worker(*args, **kwargs):
        process = popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(observer, "require_external_launcher", lambda: None)
    monkeypatch.setattr(observer, "OwnedProcess", sleeping_worker)
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
    monkeypatch.setattr(observer, "require_external_launcher", lambda: None)
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
    # Use the actual bounded transport with a fake connection, never Azure Relay.
    original_poll = observer.poll_once

    def transport(url, request):
        assert url.startswith("https://example.servicebus.windows.net/box_windows")
        chunks = iter([wire(), b""])
        return original_poll(
            url,
            request,
            connection_factory=lambda *a, **k: SimpleNamespace(
                sock=None,
                request=lambda *a, **k: calls.append(request),
                getresponse=lambda: SimpleNamespace(
                    status=200,
                    getheader=lambda key, default=None: default,
                    read1=lambda amount: next(chunks),
                    close=lambda: None,
                ),
                close=lambda: None,
            ),
        )

    monkeypatch.setattr(observer, "poll_once", transport)
    result = observer.worker({"bindings": bindings(), "settings": str(settings), "relays": str(relays)})
    assert result["outcome"] == "retained-terminal-result-observed"
    assert len(calls) == 1 and calls[0]["code"] == ""
    assert closed == [True]


def test_sql_snapshots_are_exact_key_serializable_rollback_close_only(monkeypatch):
    calls = []
    value = snapshot()
    results = iter(
        [
            [(value["record_owner"], value["revision"], json.dumps(value["job"]))],
            [
                ("hub", "oldowner", json.dumps(value["hub"])),
                ("lease", "lease", json.dumps(value["lease"])),
                ("client", "client", json.dumps(value["client"])),
                ("config", "targets", json.dumps(value["targets"])),
            ],
            [(value["database"], value["now"])],
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
    assert queries[1][1][-1] == "hubjob"
    assert queries[2][1][-3:] == ("oldowner", "lease", "client")
    assert "SYSUTCDATETIME" in queries[-1][0]
    assert all(query.startswith(("SELECT", "SET")) for query, _ in queries)
    assert calls[-4:] == ["cursor-close", "rollback", "connection-close", "credential-close"]


def test_expiry_during_locked_reads_uses_final_database_time(monkeypatch):
    backend = object.__new__(observer.ReadOnlySql)
    backend.expected = bindings()
    value = snapshot()
    queries = []
    from contextlib import contextmanager

    class Cursor:
        def execute(self, sql, *args):
            queries.append(sql)

        def fetchone(self):
            if "SYSUTCDATETIME" in queries[-1]:
                return "privy", 201  # Lease expired while the earlier reads waited.
            return "oldowner", 7, json.dumps(value["job"])

        def fetchall(self):
            return [
                ("hub", "oldowner", json.dumps(value["hub"])),
                ("lease", "lease", json.dumps(value["lease"])),
                ("client", "client", json.dumps(value["client"])),
                ("config", "targets", json.dumps(value["targets"])),
            ]

    @contextmanager
    def read():
        yield Cursor()

    backend._read = read
    actual = backend.snapshot()
    assert "SYSUTCDATETIME" in queries[-1]
    with pytest.raises(observer.Inconclusive, match="unexpired"):
        observer.validate_snapshot(actual, bindings())


def test_receipt_worker_hashes_on_python39_without_file_digest(monkeypatch, capsys):
    monkeypatch.setattr(observer, "require_external_launcher", lambda: None)
    monkeypatch.setattr(
        observer, "worker", lambda document: {"outcome": "inconclusive", "reason": "synthetic"}
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))
    monkeypatch.delattr(hashlib, "file_digest", raising=False)
    observer.worker_main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["artifact_sha256"] == hashlib.sha256(Path(observer.__file__).read_bytes()).hexdigest()
    assert receipt["artifact_kind"] == "observer-source-module"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object containment")
def test_credential_style_grandchild_is_killed_with_owned_root(tmp_path):
    import ctypes
    from ctypes import wintypes

    pidfile = tmp_path / "owned-child.pid"
    script = (
        "import subprocess,sys,time,pathlib;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid));"
        "time.sleep(30)"
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    with observer.OwnedProcess([sys.executable, "-c", script]) as process:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        handle = kernel.OpenProcess(0x100000, False, pid)
        assert handle
        try:
            started = time.monotonic()
            process.close_tree()
            process.wait(timeout=2)
            assert kernel.WaitForSingleObject(handle, 2000) == 0
            assert time.monotonic() - started < 3
        finally:
            kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object containment")
def test_assignment_failure_never_runs_uncontained_helper(tmp_path, monkeypatch):
    from privy import observer_process

    marker = tmp_path / "must-not-run"
    actual = observer_process._windows()

    class FailingKernel:
        def __getattr__(self, name):
            return getattr(actual, name)

        def AssignProcessToJobObject(self, job, process):
            return False

    monkeypatch.setattr(observer_process, "_windows", lambda: FailingKernel())
    with pytest.raises(observer_process.ContainmentError, match="assignment"):
        observer_process.OwnedProcess([sys.executable, "-c", f"open({str(marker)!r},'w').write('bad')"])
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows external native launcher")
def test_external_launcher_contains_bootstrap_and_descendants(tmp_path):
    source = Path(__file__).parents[1] / "scripts" / "Invoke-JobObserver.ps1"
    script = tmp_path / "native-launcher-test.ps1"
    script.write_text(
        "$ErrorActionPreference='Stop'; $content=Get-Content -Raw -LiteralPath $args[0]; "
        "$code=[regex]::Match($content,\"(?s)Add-Type -TypeDefinition @'\\r?\\n(.*?)\\r?\\n'@\").Groups[1].Value;"
        "Add-Type -TypeDefinition $code;"
        "$run=$null; try {"
        "$run=[PrivyObserverLaunch]::new($args[1],@('-c',$args[2]));"
        "$ready=[Diagnostics.Stopwatch]::StartNew();"
        "while(!(Test-Path -LiteralPath $args[3]) -and $ready.ElapsedMilliseconds -lt 5000){Start-Sleep -Milliseconds 20};"
        "if($run.WaitBounded(150) -ne 124){throw 'Expected synthetic bootstrap deadline'};"
        "} finally { if($null -ne $run){$run.Dispose()} };"
    )
    pidfile = tmp_path / "external-owned-child.pid"
    child = (
        "import subprocess,sys,time,pathlib;print('native-launch-contained',flush=True);"
        f"sys.path.insert(0,{str(Path(observer.__file__).parents[1])!r});"
        "from privy.observer_process import require_external_launcher;require_external_launcher();"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid));time.sleep(30)"
    )
    result = observer.subprocess.run(
        ["powershell", "-NoProfile", "-File", str(script), str(source), sys.executable, child, str(pidfile)],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"native-launch-contained" in result.stdout
    assert pidfile.exists()
    # Only the exact synthetic descendant recorded by our fixture is examined.
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x100000, False, int(pidfile.read_text()))
    if handle:
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            assert kernel.WaitForSingleObject(handle, 2000) == 0
        finally:
            kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows full launcher preflight")
@pytest.mark.parametrize("case", ["relative", "unc", "device", "provider", "mapped", "reparse"])
def test_full_launcher_preflight_locality_and_powershell_location(tmp_path, case):
    source = Path(__file__).parents[1] / "scripts" / "Invoke-JobObserver.ps1"
    directory = tmp_path / "ps-location"
    directory.mkdir()
    for name in ("observer.exe", "bindings.yaml", "settings.yaml", "relays.json"):
        (directory / name).write_text("synthetic-no-execution")
    harness = tmp_path / "preflight.ps1"
    harness.write_text(
        "$ErrorActionPreference='Stop';"
        "$content=Get-Content -Raw -LiteralPath $args[0];"
        "$prefix=$content.Substring(0,$content.IndexOf(\"$\"+'arguments ='));"
        "$case=$args[2];Set-Location -LiteralPath $args[1];"
        "$hash=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([IO.File]::ReadAllBytes((Join-Path $args[1] 'observer.exe')))).Replace('-','');"
        "$global:access=0;$global:hashes=0;"
        "function Get-Item {param($LiteralPath,[switch]$Force) $global:access++;"
        "if($LiteralPath.StartsWith('\\\\')){throw 'Remote metadata access attempted'};"
        "if($case -eq 'mapped'){throw 'Remote file access attempted'};"
        "if($case -eq 'reparse' -and $LiteralPath.EndsWith('settings.yaml')){"
        "return [pscustomobject]@{Attributes=[IO.FileAttributes]::ReparsePoint;PSIsContainer=$false}};"
        "Microsoft.PowerShell.Management\\Get-Item -LiteralPath $LiteralPath -Force:$Force};"
        "function Get-FileHash {param($LiteralPath,$Algorithm) $global:hashes++;"
        "[pscustomobject]@{Hash=$hash}};"
        "$inputPath='.\\settings.yaml';"
        "switch($case){"
        "'unc' {$inputPath='\\\\uncontacted.invalid\\share\\settings.yaml'}"
        "'device' {$inputPath='\\\\?\\UNC\\uncontacted.invalid\\share\\settings.yaml'}"
        "'provider' {$inputPath='Registry::HKEY_CURRENT_USER\\Software'}"
        "'mapped' {$prefix=$prefix.Replace('[PrivyObserverLaunch]::IsLocalDrive($root)','$false');$inputPath='.\\observer.exe'}"
        "};"
        "try {"
        "if($case -eq 'mapped'){"
        "& ([scriptblock]::Create($prefix)) -Executable $inputPath -ExpectedSha256 $hash -Bindings '.\\bindings.yaml' -Settings '.\\settings.yaml' -Relays '.\\relays.json'"
        "} else {"
        "& ([scriptblock]::Create($prefix)) -Executable '.\\observer.exe' -ExpectedSha256 $hash -Bindings '.\\bindings.yaml' -Settings $inputPath -Relays '.\\relays.json'"
        "};"
        "if($case -ne 'relative'){throw 'Disallowed path accepted'};"
        "if($global:hashes -ne 1){throw 'Expected one local artifact hash'};"
        "Write-Output 'LOCAL_PREFLIGHT_PASSED';"
        "} catch {"
        "if($case -eq 'relative'){throw};"
        "if($global:hashes -ne 0){throw 'Content hash before locality rejection'};"
        "if($case -eq 'mapped' -and $global:access -ne 0){throw 'Mapped remote accessed'};"
        "Write-Output ('REFUSED:'+$_)"
        "}"
    )
    result = observer.subprocess.run(
        ["powershell", "-NoProfile", "-File", str(harness), str(source), str(directory), case],
        cwd=str(tmp_path),
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert (b"LOCAL_PREFLIGHT_PASSED" if case == "relative" else b"REFUSED:") in result.stdout


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
