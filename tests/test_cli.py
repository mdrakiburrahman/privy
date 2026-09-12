import io
import json
import logging
import os
from types import SimpleNamespace

import pytest

from privy import _relay
from privy.batch import BatchResult, CommandOutcome
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

    def run_many(self, commands, *, max_parallel):
        self.calls.append(("batch", tuple(commands), {"max_parallel": max_parallel}))
        return BatchResult(
            outcomes=(CommandOutcome(id=commands[0].id, state="succeeded", result=_result()),),
            duration_ms=1,
        )

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
