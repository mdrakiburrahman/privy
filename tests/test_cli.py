import hashlib
import io
import json
import logging
import os
import threading
from types import SimpleNamespace

import pytest

from privy import _relay
from privy.batch import BatchCallbackError, BatchResult, CommandOutcome, run_many
from privy.batch_results import BatchResultsError, BatchResultsWriter
from privy.cli import (
    TOKEN_EXPIRED_EXIT_CODE,
    CliError,
    _configure_logging,
    _emit,
    _guard_packaged_server,
    _reexec_server,
    _RelayRedactionFilter,
    build_parser,
    main,
)
from privy.client import ExecResult
from privy.transfer import TransferResult


def _result(**overrides):
    values = {
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "stdout_bytes": b"",
        "stderr_bytes": b"",
        "duration_ms": 1,
        "timed_out": False,
        "error": None,
        "job_id": None,
    }
    values.update(overrides)
    return ExecResult(**values)


def test_emit_writes_remote_bytes_without_windows_text_translation(monkeypatch):
    stdout_bytes = io.BytesIO()
    stderr_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="ascii", newline="\r\n")
    stderr = io.TextIOWrapper(stderr_bytes, encoding="ascii", newline="\r\n")
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)
    result = _result(
        stdout="café 世界\r\n",
        stderr="échec\r\n",
        stdout_bytes="café 世界\r\n".encode(),
        stderr_bytes="échec\r\n".encode(),
    )

    assert _emit(result, as_json=False) == 0
    assert stdout_bytes.getvalue() == "café 世界\r\n".encode()
    assert stderr_bytes.getvalue() == "échec\r\n".encode()


class FakeClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.instances.append(self)

    def run_bash(self, code, **kwargs):
        self.calls.append(("bash", code, kwargs))
        return _result(stdout="bash\n", stdout_bytes=b"bash\n")

    def run_python(self, code, **kwargs):
        self.calls.append(("python", code, kwargs))
        return _result(stdout="python\n", stdout_bytes=b"python\n")

    def run_powershell(self, code, **kwargs):
        self.calls.append(("powershell", code, kwargs))
        return _result(stdout="powershell\n", stdout_bytes=b"powershell\n")

    def run_many(self, commands, *, max_parallel, on_command_complete=None):
        options = {"max_parallel": max_parallel}
        if on_command_complete is not None:
            options["on_command_complete"] = on_command_complete
        self.calls.append(("batch", tuple(commands), options))
        result = BatchResult(
            outcomes=tuple(
                CommandOutcome(id=command.id, state="succeeded", result=_result())
                for command in commands
            ),
            duration_ms=1,
        )
        if on_command_complete is not None:
            for outcome in result.outcomes:
                on_command_complete(outcome)
        return result

    def upload_file(self, local, remote, **kwargs):
        self.calls.append(("upload", local, remote, kwargs))
        return TransferResult("upload", local, remote, 3, "a" * 64, "transfer")

    def download_file(self, remote, local, **kwargs):
        self.calls.append(("download", remote, local, kwargs))
        return TransferResult("download", remote, local, 3, "a" * 64, "transfer")


@pytest.fixture
def relay_args():
    return [
        "--namespace",
        "ns",
        "--path",
        "path",
        "--keyrule",
        "rule",
        "--key",
        "key",
    ]


def test_top_level_help_recursively_exposes_every_command():
    help_text = build_parser().format_help()

    for command in (
        "server",
        "client",
        "proxy",
        "file",
        "file upload",
        "file download",
        "token",
        "token mint",
    ):
        assert f"FULL COMMAND HELP: privy {command}" in help_text
    for option in (
        "--timeout-s",
        "--async-job",
        "--batch",
        "--powershell",
        "--chunk-size",
        "--token",
        "--ttl-seconds",
        "--rights",
    ):
        assert option in help_text
    assert "55s automatically use submit + long-poll" in help_text
    assert "RelayClient.submit(), poll(), and cancel()" in help_text


@pytest.mark.parametrize(
    ("flag", "method"),
    [("--bash", "bash"), ("--python", "python"), ("--powershell", "powershell")],
)
def test_cli_passes_timeout_to_execution_target(monkeypatch, capsys, relay_args, flag, method):
    FakeClient.instances.clear()
    monkeypatch.setattr("privy.cli.RelayClient", FakeClient)

    assert main(["client", *relay_args, flag, "echo", "--timeout-s", "42"]) == 0

    call = FakeClient.instances[-1].calls[-1]
    assert call[0] == method
    assert call[2]["timeout_s"] == 42
    assert call[2]["async_job"] is None
    capsys.readouterr()


def test_cli_dispatches_powershell_file(monkeypatch, tmp_path, capsys, relay_args):
    FakeClient.instances.clear()
    monkeypatch.setattr("privy.cli.RelayClient", FakeClient)
    script = tmp_path / "script.ps1"
    script.write_text("Write-Output 42", encoding="utf-8")

    assert (
        main(
            [
                "client",
                *relay_args,
                "--file",
                str(script),
                "--file-kind",
                "powershell",
                "--timeout-s",
                "1",
            ]
        )
        == 0
    )
    assert FakeClient.instances[-1].calls[-1][:2] == ("powershell", "Write-Output 42")
    capsys.readouterr()


def test_cli_batch_reads_json_from_stdin(monkeypatch, capsys, relay_args):
    FakeClient.instances.clear()
    monkeypatch.setattr("privy.cli.RelayClient", FakeClient)
    monkeypatch.setattr(
        "sys.stdin",
        type(
            "Input",
            (),
            {
                "read": lambda self: json.dumps(
                    {
                        "max_parallel": 7,
                        "commands": [{"id": "one", "kind": "bash", "code": "true"}],
                    }
                )
            },
        )(),
    )

    assert main(["client", *relay_args, "--batch", "-", "--json"]) == 0

    call = FakeClient.instances[-1].calls[-1]
    assert call[0] == "batch"
    assert call[2]["max_parallel"] == 7
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_batch_without_results_flag_does_not_construct_sink_or_pass_hook(
    monkeypatch, tmp_path, capsys, relay_args,
):
    FakeClient.instances.clear()
    monkeypatch.setattr("privy.cli.RelayClient", FakeClient)

    def forbidden_sink(*args, **kwargs):
        pytest.fail("batch without --results-dir must not construct an artifact sink")

    monkeypatch.setattr("privy.batch_results.BatchResultsWriter", forbidden_sink)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"commands":[{"id":"one","kind":"bash","code":"true"}]}'))

    assert main(["client", *relay_args, "--batch", "-", "--json"]) == 0

    assert FakeClient.instances[-1].calls[-1][2] == {"max_parallel": 32}
    assert list(tmp_path.iterdir()) == []
    assert json.loads(capsys.readouterr().out) == BatchResult(
        (CommandOutcome("one", "succeeded", _result()),), 1,
    ).to_dict()


@pytest.mark.skipif(os.name != "posix", reason="private results directories require POSIX")
@pytest.mark.parametrize("as_json", [False, True])
def test_cli_results_leave_final_output_unchanged(monkeypatch, tmp_path, capsys, relay_args, as_json):
    FakeClient.instances.clear()
    monkeypatch.setattr("privy.cli.RelayClient", FakeClient)
    path = tmp_path / "manifest.json"
    raw = b'{\r\n"max_parallel":7,"commands":[{"id":"one","kind":"bash","code":"true"}]\r\n}\r\n'
    path.write_bytes(raw)
    directory = tmp_path / "results"
    arguments = ["client", *relay_args, "--batch", str(path)]
    if as_json:
        arguments.append("--json")
    assert main(arguments) == 0
    before = capsys.readouterr()

    assert main([*arguments, "--results-dir", str(directory)]) == 0

    after = capsys.readouterr()
    assert after == before
    metadata = json.loads((directory / "batch.json").read_text())
    assert metadata["manifest"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert metadata["manifest"]["source"] == str(path)
    assert metadata["manifest"]["max_parallel"] == 7
    marker = json.loads((directory / "complete.json").read_text())
    assert marker["state"] == "complete"
    assert marker["artifact_count"] == 1
    assert marker["result"] == BatchResult((CommandOutcome("one", "succeeded", _result()),), 1).to_dict()
    if as_json:
        assert marker["result"] == json.loads(after.out)


def test_cli_results_flag_requires_batch_before_client_construction(monkeypatch, tmp_path, capsys, relay_args):
    FakeClient.instances.clear()
    monkeypatch.setattr("privy.cli.RelayClient", FakeClient)
    directory = tmp_path / "results"

    assert main(["client", *relay_args, "--python", "print(1)", "--results-dir", str(directory)]) == 2

    assert FakeClient.instances == []
    assert not directory.exists()
    assert "--results-dir requires --batch" in capsys.readouterr().err


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"\xff",
        b"[]",
        b'{"commands":[]}',
        b'{"commands":[{"id":"one","kind":"bash","code":"unused","depends_on":["unknown"]}]}',
        b'{"commands":[{"id":"one","kind":"bash","code":"unused","timeout_s":NaN}]}',
        b'{"max_parallel":0,"commands":[{"id":"one","kind":"bash","code":"unused"}]}',
        b'{"commands":[{"id":"one","kind":"bash","code":"unused"},{"id":"one","kind":"bash","code":"unused"}]}',
        b'{"commands":[{"id":"a","kind":"bash","code":"unused","depends_on":["b"]},'
        b'{"id":"b","kind":"bash","code":"unused","depends_on":["a"]}]}',
    ],
)
def test_cli_results_reject_malformed_manifest_before_sink_or_execution(
    monkeypatch, tmp_path, capsys, relay_args, raw,
):
    FakeClient.instances.clear()
    monkeypatch.setattr("privy.cli.RelayClient", FakeClient)
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    directory = tmp_path / "results"

    assert main(["client", *relay_args, "--batch", str(path), "--results-dir", str(directory)]) == 2

    assert FakeClient.instances[-1].calls == []
    assert not directory.exists()
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(os.name != "posix", reason="private results directories require POSIX")
@pytest.mark.parametrize("failure", ["existing", "metadata", "async-option"])
def test_cli_results_preflight_failure_never_runs_commands(
    monkeypatch, tmp_path, capsys, relay_args, failure,
):
    FakeClient.instances.clear()
    monkeypatch.setattr("privy.cli.RelayClient", FakeClient)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"commands":[{"id":"one","kind":"bash","code":"unused"}]}'))
    directory = tmp_path / "results"
    arguments = ["client", *relay_args, "--batch", "-", "--results-dir", str(directory)]
    if failure == "existing":
        directory.mkdir()
    elif failure == "metadata":
        def cannot_publish(*args, **kwargs):
            raise BatchResultsError("metadata disk unavailable")

        monkeypatch.setattr(BatchResultsWriter, "_publish", cannot_publish)
    else:
        arguments.append("--async-job")

    assert main(arguments) == 2

    assert FakeClient.instances[-1].calls == []
    assert not (directory / "command-000001.json").exists()
    assert capsys.readouterr().out == ""


class NativeBatchClient(FakeClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fast_terminal = threading.Event()
        self.submitted = []
        self.active = set()
        self.cancelled = []
        self.notified = []
        self.native_result = None

    def submit(self, request):
        self.submitted.append(request.code)
        self.active.add(request.code)
        return request.code

    def poll(self, request, job_id, *, wait_s):
        if job_id == "slow":
            assert self.fast_terminal.wait(5)
        self.active.remove(job_id)
        return "done", _result(stdout=job_id, stdout_bytes=job_id.encode(), job_id=job_id)

    def cancel(self, request, job_id):
        self.cancelled.append(job_id)

    def run_many(self, commands, *, max_parallel, on_command_complete=None):
        def completed(outcome):
            self.notified.append(outcome)
            try:
                if on_command_complete is not None:
                    on_command_complete(outcome)
            finally:
                if outcome.id == "fast":
                    self.fast_terminal.set()

        try:
            self.native_result = run_many(
                self, commands, max_parallel=max_parallel, on_command_complete=completed,
            )
        except BatchCallbackError as exc:
            self.native_result = exc.result
            raise
        return self.native_result


@pytest.mark.skipif(os.name != "posix", reason="private results directories require POSIX")
@pytest.mark.parametrize("with_pending", [False, True])
def test_cli_sink_failure_drains_jobs_and_keeps_native_final_json(
    monkeypatch, tmp_path, capsys, relay_args, with_pending,
):
    FakeClient.instances.clear()
    monkeypatch.setattr("privy.cli.RelayClient", NativeBatchClient)
    ids = ["fast", "slow", "pending"] if with_pending else ["fast", "slow"]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "max_parallel": 2,
        "commands": [{"id": name, "kind": "bash", "code": name} for name in ids],
    })))
    directory = tmp_path / "results"
    publish = BatchResultsWriter._publish

    def fail_first_receipt(self, filename, value):
        if filename == "command-000001.json":
            raise BatchResultsError("terminal result disk unavailable")
        return publish(self, filename, value)

    monkeypatch.setattr(BatchResultsWriter, "_publish", fail_first_receipt)

    assert main(["client", *relay_args, "--batch", "-", "--json", "--results-dir", str(directory)]) == 1

    captured = capsys.readouterr()
    client = FakeClient.instances[-1]
    native = json.loads(captured.out)
    assert native == client.native_result.to_dict()
    assert native["commands"][0]["state"] == "succeeded"
    assert native["commands"][1]["result"]["stdout"] == "slow"
    assert native["ok"] is (not with_pending)
    assert client.active == set()
    assert client.cancelled == []
    assert client.submitted == ["fast", "slow"]
    assert len(client.notified) == len(ids)
    assert "terminal result disk unavailable" in captured.err
    marker = json.loads((directory / "complete.json").read_text())
    assert marker["state"] == "error"
    assert marker["result"] == native
    assert marker["missing_command_ids"] == ["fast"]
    assert marker["artifact_count"] == len(ids) - 1


@pytest.mark.skipif(os.name != "posix", reason="private results directories require POSIX")
def test_cli_final_marker_failure_cannot_suppress_native_stdout(monkeypatch, tmp_path, capsys, relay_args):
    monkeypatch.setattr("privy.cli.RelayClient", FakeClient)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"commands":[{"id":"one","kind":"bash","code":"unused"}]}'))
    directory = tmp_path / "results"
    publish = BatchResultsWriter._publish

    def fail_marker(self, filename, value):
        if filename == "complete.json":
            raise BatchResultsError("final marker disk unavailable")
        return publish(self, filename, value)

    monkeypatch.setattr(BatchResultsWriter, "_publish", fail_marker)

    assert main(["client", *relay_args, "--batch", "-", "--json", "--results-dir", str(directory)]) == 1

    captured = capsys.readouterr()
    assert json.loads(captured.out) == BatchResult((CommandOutcome("one", "succeeded", _result()),), 1).to_dict()
    assert "final marker disk unavailable" in captured.err
    assert (directory / "command-000001.json").exists()
    assert not (directory / "complete.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="private results directories require POSIX")
@pytest.mark.parametrize("sink_fails", [False, True])
def test_cli_interruption_preserves_exit_and_terminal_artifacts(
    monkeypatch, tmp_path, capsys, relay_args, sink_fails,
):
    class InterruptingClient(NativeBatchClient):
        def poll(self, request, job_id, *, wait_s):
            raise KeyboardInterrupt

    monkeypatch.setattr("privy.cli.RelayClient", InterruptingClient)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "max_parallel": 1,
        "commands": [{"id": name, "kind": "bash", "code": name} for name in ("one", "pending")],
    })))
    directory = tmp_path / "results"
    if sink_fails:
        publish = BatchResultsWriter._publish

        def fail_receipts(self, filename, value):
            if filename.startswith("command-"):
                raise BatchResultsError("cancel receipt unavailable")
            return publish(self, filename, value)

        monkeypatch.setattr(BatchResultsWriter, "_publish", fail_receipts)

    assert main(["client", *relay_args, "--batch", "-", "--json", "--results-dir", str(directory)]) == 130

    captured = capsys.readouterr()
    native = json.loads(captured.out)
    assert [outcome["state"] for outcome in native["commands"]] == ["cancelled", "cancelled"]
    assert FakeClient.instances[-1].cancelled == ["one"]
    marker = json.loads((directory / "complete.json").read_text())
    assert marker["state"] == "interrupted"
    assert marker["result"] == native
    assert marker["artifact_count"] == (0 if sink_fails else 2)


def test_cli_timeout_uses_shell_conventional_exit_124(monkeypatch, capsys, relay_args):
    monkeypatch.setattr("privy.cli.RelayClient", FakeClient)
    monkeypatch.setattr(
        FakeClient,
        "run_bash",
        lambda self, code, **kwargs: _result(exit_code=-9, timed_out=True, error="timeout"),
    )

    assert main(["client", *relay_args, "--bash", "sleep 10", "--timeout-s", "1"]) == 124

    assert "timeout" in capsys.readouterr().err


def test_cli_file_upload_dispatches_transfer_options(monkeypatch, capsys, relay_args):
    FakeClient.instances.clear()
    monkeypatch.setattr("privy.cli.RelayClient", FakeClient)

    assert (
        main(
            [
                "file",
                "upload",
                *relay_args,
                "local.bin",
                "/tmp/remote.bin",
                "--chunk-size",
                "64KiB",
                "--overwrite",
                "--json",
            ]
        )
        == 0
    )

    call = FakeClient.instances[-1].calls[-1]
    assert call[:3] == ("upload", "local.bin", "/tmp/remote.bin")
    assert call[3]["chunk_size"] == 64 * 1024
    assert call[3]["overwrite"] is True
    assert json.loads(capsys.readouterr().out)["direction"] == "upload"


def test_token_mint_uses_role_specific_environment(monkeypatch, capsys):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    monkeypatch.setenv("PRIVY_RELAY_NAMESPACE", "ns")
    monkeypatch.setenv("PRIVY_RELAY_PATH", "path")
    monkeypatch.setenv("PRIVY_RELAY_SEND_KEYRULE", "send-rule")
    monkeypatch.setenv("PRIVY_RELAY_SEND_KEY", "send-key")

    assert main(["token", "mint", "--rights", "send", "--ttl", "30m"]) == 0

    claims = _relay.parse_sas_token(capsys.readouterr().out.strip())
    assert claims["skn"] == "send-rule"
    assert claims["se"] == 1_800_001_800


def test_cli_rejects_token_and_key_credentials_together(monkeypatch, capsys):
    monkeypatch.setenv("PRIVY_RELAY_TOKEN", "token")
    monkeypatch.setenv("PRIVY_RELAY_KEYRULE", "rule")
    monkeypatch.setenv("PRIVY_RELAY_KEY", "key")

    code = main(["client", "--namespace", "ns", "--path", "path", "--bash", "true"])

    assert code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_cli_maps_expired_listener_token_to_distinct_exit(monkeypatch, capsys):
    class ExpiredServer:
        def __init__(self, **kwargs):
            pass

        def serve_forever(self):
            raise _relay.RelayTokenExpiredError(1_800_000_000)

    monkeypatch.setattr("privy.cli.RelayServer", ExpiredServer)
    monkeypatch.setattr("privy.cli._server_reexec_required", lambda: False)
    monkeypatch.setattr("privy.cli._harden_server_process", lambda: None)
    monkeypatch.setenv("PRIVY_RELAY_TOKEN", "unused")

    code = main(["server", "--namespace", "ns", "--path", "path"])

    assert code == TOKEN_EXPIRED_EXIT_CODE
    assert "relay token expired" in capsys.readouterr().err


def test_verbose_logging_is_scoped_away_from_http_libraries():
    _configure_logging(2)

    assert logging.getLogger("privy").level == logging.DEBUG
    assert logging.getLogger("urllib3").level == logging.WARNING
    assert logging.getLogger("requests").level == logging.WARNING
    assert logging.getLogger("websocket").level == logging.WARNING
    assert logging.getLogger().level == logging.WARNING


def test_log_filter_redacts_token_bearing_urls():
    record = logging.LogRecord(
        "privy.test",
        logging.DEBUG,
        __file__,
        1,
        "POST https://ns/path?sb-hc-token=secret%26sig%3Dvalue",
        (),
        None,
    )

    assert _RelayRedactionFilter().filter(record)
    assert record.getMessage().endswith("sb-hc-token=<redacted>")


def test_server_cli_removes_credentials_from_process_environment(monkeypatch, relay_args):
    captured = {}

    class FakeServer:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def serve_forever(self):
            assert "PRIVY_RELAY_KEY" not in os.environ
            assert "PRIVY_RELAY_TOKEN" not in os.environ
            assert "STORAGE_KEY" not in os.environ

    monkeypatch.setattr("privy.cli.RelayServer", FakeServer)
    monkeypatch.setattr("privy.cli._server_reexec_required", lambda: False)
    monkeypatch.setattr("privy.cli._harden_server_process", lambda: None)
    monkeypatch.setenv("PRIVY_RELAY_TOKEN", "")
    monkeypatch.setenv("PRIVY_RELAY_KEY", "key")
    monkeypatch.setenv("STORAGE_KEY", "storage-secret")

    assert main(["server", *relay_args]) == 0
    assert captured["key"] == "key"


def test_server_reexec_uses_pipe_and_sanitized_process_metadata(monkeypatch):
    captured = {}
    relay = {
        "namespace": "ns",
        "path": "path",
        "token": "SharedAccessSignature secret",
        "ttl_seconds": 60,
    }
    args = SimpleNamespace(
        max_workers=4,
        recv_timeout_s=1.5,
        proxy_target="http://127.0.0.1:8000",
        verbose=0,
        verbose_sub=2,
    )
    monkeypatch.setenv("PRIVY_RELAY_TOKEN", "environment-secret")
    monkeypatch.setenv("STORAGE_KEY", "storage-secret")

    def fake_execve(executable, argv, environment):
        fd = int(argv[argv.index("--credential-fd") + 1])
        captured["executable"] = executable
        captured["argv"] = argv
        captured["environment"] = environment
        captured["payload"] = json.loads(os.read(fd, 64 * 1024))

    monkeypatch.setattr("privy.cli.os.execve", fake_execve)

    with pytest.raises(RuntimeError, match="unexpectedly returned"):
        _reexec_server(relay, args)

    assert captured["payload"]["relay"]["token"] == "SharedAccessSignature secret"
    assert "SharedAccessSignature secret" not in " ".join(captured["argv"])
    assert "PRIVY_RELAY_TOKEN" not in captured["environment"]
    assert "STORAGE_KEY" not in captured["environment"]
    assert "-vv" in captured["argv"]


def test_packaged_server_reexec_enters_private_proc_namespace(monkeypatch):
    captured = {}
    relay = {
        "namespace": "ns",
        "path": "path",
        "keyrule": "rule",
        "key": "secret",
        "ttl_seconds": 60,
    }
    args = SimpleNamespace(
        max_workers=1,
        recv_timeout_s=1.0,
        proxy_target=None,
        verbose=0,
        verbose_sub=0,
    )
    monkeypatch.setattr("privy.cli.sys.frozen", True, raising=False)
    monkeypatch.setattr("privy.cli.sys.platform", "linux")
    monkeypatch.setattr("privy.cli.os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("privy.cli.shutil.which", lambda command: "/usr/bin/unshare")

    def fake_execve(executable, argv, environment):
        fd = int(argv[argv.index("--credential-fd") + 1])
        captured["executable"] = executable
        captured["argv"] = argv
        captured["payload"] = json.loads(os.read(fd, 64 * 1024))

    monkeypatch.setattr("privy.cli.os.execve", fake_execve)

    with pytest.raises(RuntimeError, match="unexpectedly returned"):
        _reexec_server(relay, args)

    assert captured["executable"] == "/usr/bin/unshare"
    assert captured["argv"][:7] == [
        "/usr/bin/unshare",
        "--user",
        "--map-current-user",
        "--pid",
        "--fork",
        "--kill-child=KILL",
        "--mount-proc",
    ]
    assert captured["payload"]["relay"]["key"] == "secret"
    assert "secret" not in " ".join(captured["argv"])


def test_packaged_server_refuses_root(monkeypatch):
    args = SimpleNamespace(
        _credential_fd=0,
        max_workers=1,
        recv_timeout_s=1.0,
        proxy_target=None,
        verbose=0,
        verbose_sub=0,
    )
    monkeypatch.setattr("privy.cli.sys.frozen", True, raising=False)
    monkeypatch.setattr("privy.cli.sys.platform", "linux")
    monkeypatch.setattr("privy.cli.os.geteuid", lambda: 0, raising=False)

    with pytest.raises(CliError, match="refuses to run as root"):
        _guard_packaged_server(args)

    with pytest.raises(CliError, match="refuses to run as root"):
        _reexec_server(
            {
                "namespace": "ns",
                "path": "path",
                "keyrule": "rule",
                "key": "secret",
                "ttl_seconds": 60,
            },
            args,
        )


def test_packaged_server_rejects_direct_credential_descriptor(monkeypatch):
    monkeypatch.setattr("privy.cli.sys.frozen", True, raising=False)
    monkeypatch.setattr("privy.cli.sys.platform", "linux")
    monkeypatch.setattr("privy.cli.os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("privy.cli.os.getpid", lambda: 123)

    with pytest.raises(CliError, match="requires private PID isolation"):
        _guard_packaged_server(SimpleNamespace(_credential_fd=0))
