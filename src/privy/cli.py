"""Command-line interface for privy's Relay server, client, and utilities."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import math
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from privy import __version__
from privy._relay import (
    DEFAULT_TOKEN_TTL_SECONDS,
    RELAY_SECRET_ENV_VARS,
    RelayTokenError,
    RelayTokenExpiredError,
    create_sas_token,
    redact_relay_secrets,
)
from privy.batch import BatchResult, BatchValidationError, parse_batch_manifest
from privy.client import RELAY_RESPONSE_LIMIT_S, ExecResult, RelayClient
from privy.protocol import DEFAULT_TIMEOUT_S
from privy.proxy import ProxyClientServer
from privy.server import RelayServer
from privy.transfer import DEFAULT_CHUNK_SIZE, TransferError, TransferResult

TOKEN_EXPIRED_EXIT_CODE = 3
_SERVER_SECRET_ENV_VARS = (*RELAY_SECRET_ENV_VARS, "BASE64_ENV", "STORAGE_KEY")
_SERVER_CONFIG_MAX_BYTES = 64 * 1024
_PR_SET_DUMPABLE = 4

_BASE_RELAY_SETTINGS = (
    ("namespace", "PRIVY_RELAY_NAMESPACE"),
    ("path", "PRIVY_RELAY_PATH"),
)
_KEY_RELAY_SETTINGS = (
    ("keyrule", "PRIVY_RELAY_KEYRULE"),
    ("key", "PRIVY_RELAY_KEY"),
)
_ROLE_KEY_SETTINGS = {
    "send": ("PRIVY_RELAY_SEND_KEYRULE", "PRIVY_RELAY_SEND_KEY"),
    "listen": ("PRIVY_RELAY_LISTEN_KEYRULE", "PRIVY_RELAY_LISTEN_KEY"),
}


class CliError(Exception):
    """User-facing usage error; printed without a traceback."""


class _HelpFormatter(argparse.RawDescriptionHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=34, width=100)


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser whose help recursively documents nested commands."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", _HelpFormatter)
        super().__init__(*args, **kwargs)

    def format_help(self) -> str:
        text = super().format_help().rstrip()
        for action in self._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            seen: set[int] = set()
            for child in action.choices.values():
                if id(child) in seen:
                    continue
                seen.add(id(child))
                title = f"FULL COMMAND HELP: {child.prog}"
                text += f"\n\n{title}\n{'-' * len(title)}\n{child.format_help().rstrip()}"
        return text + "\n"


class _RelayRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        redacted = redact_relay_secrets(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add verbosity to a command so it works on either side of the subcommand."""
    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose_sub",
        action="count",
        default=0,
        help="increase log verbosity (-v for INFO, -vv for DEBUG)",
    )


def _add_relay_args(parser: argparse.ArgumentParser) -> None:
    _add_common_args(parser)
    group = parser.add_argument_group(
        "relay connection",
        "Provide --token, or provide both --keyrule and --key. Flags override matching env vars.",
    )
    for dest, env in _BASE_RELAY_SETTINGS:
        group.add_argument(
            f"--{dest}",
            default=None,
            metavar=dest.upper(),
            help=f"Azure Relay {dest} (env: {env})",
        )
    group.add_argument(
        "--token",
        default=None,
        metavar="SAS",
        help="pre-minted SAS token (env: PRIVY_RELAY_TOKEN); exclusive with key credentials",
    )
    for dest, env in _KEY_RELAY_SETTINGS:
        group.add_argument(
            f"--{dest}",
            default=None,
            metavar=dest.upper(),
            help=f"Azure Relay {dest} used to mint tokens (env: {env})",
        )
    group.add_argument(
        "--ttl-seconds",
        type=_positive_int,
        default=None,
        metavar="SECONDS",
        help=(
            "lifetime for internally minted key-based tokens "
            f"(env: PRIVY_RELAY_TTL_SECONDS; default: {DEFAULT_TOKEN_TTL_SECONDS})"
        ),
    )


def _resolve_relay(args: argparse.Namespace) -> dict[str, Any]:
    """Merge relay flags over environment variables and validate one credential shape."""
    resolved: dict[str, Any] = {}
    missing: list[str] = []
    for dest, env in _BASE_RELAY_SETTINGS:
        value = getattr(args, dest, None) or os.environ.get(env, "")
        if not value:
            missing.append(f"--{dest} (or {env})")
        else:
            resolved[dest] = value
    if missing:
        raise CliError("missing required relay settings: " + ", ".join(missing))

    token = getattr(args, "token", None) or os.environ.get("PRIVY_RELAY_TOKEN", "")
    key_values = {
        dest: getattr(args, dest, None) or os.environ.get(env, "") for dest, env in _KEY_RELAY_SETTINGS
    }
    supplied_keys = [dest for dest, value in key_values.items() if value]
    if token and supplied_keys:
        raise CliError(
            "--token/PRIVY_RELAY_TOKEN is mutually exclusive with --keyrule/--key "
            "and PRIVY_RELAY_KEYRULE/PRIVY_RELAY_KEY"
        )
    if token:
        resolved["token"] = token
    elif len(supplied_keys) == len(_KEY_RELAY_SETTINGS):
        resolved.update(key_values)
    else:
        absent = [f"--{dest} (or {env})" for dest, env in _KEY_RELAY_SETTINGS if not key_values[dest]]
        raise CliError(
            "provide --token (or PRIVY_RELAY_TOKEN), or both key settings; missing: " + ", ".join(absent)
        )

    ttl_value: int | str = getattr(args, "ttl_seconds", None) or os.environ.get(
        "PRIVY_RELAY_TTL_SECONDS",
        DEFAULT_TOKEN_TTL_SECONDS,
    )
    try:
        resolved["ttl_seconds"] = _positive_int(ttl_value)
    except argparse.ArgumentTypeError as exc:
        raise CliError(f"invalid PRIVY_RELAY_TTL_SECONDS: {exc}") from exc
    return resolved


def _resolve_mint_args(args: argparse.Namespace) -> tuple[str, str, str, str]:
    values: dict[str, str] = {}
    missing: list[str] = []
    for dest, env in _BASE_RELAY_SETTINGS:
        value = getattr(args, dest, None) or os.environ.get(env, "")
        if value:
            values[dest] = value
        else:
            missing.append(f"--{dest} (or {env})")

    keyrule_env, key_env = _ROLE_KEY_SETTINGS[args.rights]
    keyrule = args.keyrule or os.environ.get(keyrule_env, "")
    key = args.key or os.environ.get(key_env, "")
    if not keyrule:
        missing.append(f"--keyrule (or {keyrule_env})")
    if not key:
        missing.append(f"--key (or {key_env})")
    if missing:
        raise CliError("missing token mint settings: " + ", ".join(missing))
    return values["namespace"], values["path"], keyrule, key


def _server_reexec_required() -> bool:
    return os.name == "posix"


def _reexec_server(relay: dict[str, Any], args: argparse.Namespace) -> None:
    payload = json.dumps(
        {
            "relay": relay,
            "max_workers": args.max_workers,
            "recv_timeout_s": args.recv_timeout_s,
            "proxy_target": args.proxy_target,
        },
        separators=(",", ":"),
    ).encode()
    if len(payload) > _SERVER_CONFIG_MAX_BYTES:
        raise CliError("server credential payload is unexpectedly large")

    read_fd, write_fd = os.pipe()
    try:
        os.set_inheritable(read_fd, True)
        written = 0
        while written < len(payload):
            count = os.write(write_fd, payload[written:])
            if count == 0:
                raise CliError("cannot pass server credentials through the inherited pipe")
            written += count
        os.close(write_fd)
        write_fd = -1

        executable = sys.executable
        if getattr(sys, "frozen", False):
            argv = [sys.executable, "server", "--credential-fd", str(read_fd)]
            if sys.platform.startswith("linux"):
                if os.geteuid() == 0:
                    raise CliError(
                        "the packaged server refuses to run as root; use a dedicated non-root account"
                    )
                unshare = shutil.which("unshare")
                if unshare is None:
                    raise CliError("the packaged server requires util-linux 'unshare' to isolate credentials")
                executable = unshare
                argv = [
                    unshare,
                    "--user",
                    "--map-current-user",
                    "--pid",
                    "--fork",
                    "--kill-child=KILL",
                    "--mount-proc",
                    *argv,
                ]
        else:
            argv = [
                sys.executable,
                "-m",
                "privy",
                "server",
                "--credential-fd",
                str(read_fd),
            ]
        verbosity = max(args.verbose, getattr(args, "verbose_sub", 0))
        if verbosity:
            argv.append("-" + ("v" * verbosity))
        environment = dict(os.environ)
        for name in _SERVER_SECRET_ENV_VARS:
            environment.pop(name, None)
        os.execve(executable, argv, environment)
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)
    raise RuntimeError("server credential re-exec unexpectedly returned")


def _read_server_config(fd: int) -> tuple[dict[str, Any], int, float, str | None]:
    if fd < 0:
        raise CliError("server credential descriptor must be non-negative")
    try:
        with os.fdopen(fd, "rb", closefd=True) as stream:
            payload = stream.read(_SERVER_CONFIG_MAX_BYTES + 1)
    except OSError as exc:
        raise CliError(f"cannot read server credential descriptor: {exc}") from exc
    if len(payload) > _SERVER_CONFIG_MAX_BYTES:
        raise CliError("server credential payload exceeds the allowed size")
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CliError("server credential payload is invalid") from exc
    if not isinstance(value, dict) or not isinstance(value.get("relay"), dict):
        raise CliError("server credential payload has an invalid shape")

    relay = value["relay"]
    allowed_relay = {"namespace", "path", "token", "keyrule", "key", "ttl_seconds"}
    if set(relay) - allowed_relay:
        raise CliError("server credential payload contains unexpected settings")
    for name in ("namespace", "path"):
        if not isinstance(relay.get(name), str) or not relay[name]:
            raise CliError(f"server credential payload has invalid {name}")
    max_workers = _config_positive_int(value.get("max_workers"), "max_workers")
    recv_timeout_s = _config_positive_float(value.get("recv_timeout_s"), "recv_timeout_s")
    proxy_target = value.get("proxy_target")
    if proxy_target is not None and not isinstance(proxy_target, str):
        raise CliError("server credential payload has invalid proxy_target")
    return relay, max_workers, recv_timeout_s, proxy_target


def _harden_server_process() -> None:
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise CliError(
            "cannot protect server credentials from same-user process inspection: "
            + os.strerror(error_number)
        )


def _guard_packaged_server(args: argparse.Namespace) -> None:
    if not (getattr(sys, "frozen", False) and sys.platform.startswith("linux")):
        return
    if os.geteuid() == 0:
        raise CliError("the packaged server refuses to run as root; use a dedicated non-root account")
    if args._credential_fd is not None and os.getpid() != 1:
        raise CliError("the packaged server credential descriptor requires private PID isolation")


def _config_positive_int(value: Any, name: str) -> int:
    try:
        return _positive_int(value)
    except argparse.ArgumentTypeError as exc:
        raise CliError(f"server credential payload has invalid {name}: {exc}") from exc


def _config_positive_float(value: Any, name: str) -> float:
    try:
        return _positive_float(value)
    except argparse.ArgumentTypeError as exc:
        raise CliError(f"server credential payload has invalid {name}: {exc}") from exc


def _read_text(path: str, *, purpose: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read {purpose} {path}: {exc}") from exc


def _read_code(args: argparse.Namespace) -> tuple[str, str]:
    if args.bash is not None:
        return "bash", args.bash
    if args.powershell is not None:
        return "powershell", args.powershell
    if args.python is not None:
        return "python", args.python
    return args.file_kind, _read_text(args.file, purpose="code file")


def _write_bytes(stream: Any, data: bytes) -> None:
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(data)
    else:
        stream.write(data.decode("utf-8", "replace"))


def _emit(result: ExecResult, as_json: bool) -> int:
    if as_json:
        json.dump(
            {
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_ms": result.duration_ms,
                "timed_out": result.timed_out,
                "error": result.error,
                "job_id": result.job_id,
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
    else:
        if result.stdout_bytes:
            _write_bytes(sys.stdout, result.stdout_bytes)
        if result.stderr_bytes:
            _write_bytes(sys.stderr, result.stderr_bytes)
        if result.error:
            _write_bytes(sys.stderr, f"privy: {result.error}\n".encode())
    sys.stdout.flush()
    sys.stderr.flush()
    if result.timed_out:
        return 124
    return result.exit_code


def _emit_batch(result: BatchResult, as_json: bool) -> int:
    if as_json:
        json.dump(result.to_dict(), sys.stdout)
        sys.stdout.write("\n")
    else:
        for outcome in result.outcomes:
            details = ""
            if outcome.result is not None:
                details = f" exit={outcome.result.exit_code} duration={outcome.result.duration_ms}ms"
            _write_bytes(
                sys.stdout,
                f"[{outcome.state.upper()}] {outcome.id}{details}\n".encode(),
            )
            if outcome.result is not None and outcome.result.stdout_bytes:
                _write_bytes(sys.stdout, outcome.result.stdout_bytes)
                if not outcome.result.stdout_bytes.endswith(b"\n"):
                    _write_bytes(sys.stdout, b"\n")
            if outcome.result is not None and outcome.result.stderr_bytes:
                _write_bytes(sys.stderr, f"[{outcome.id} stderr]\n".encode())
                _write_bytes(sys.stderr, outcome.result.stderr_bytes)
                if not outcome.result.stderr_bytes.endswith(b"\n"):
                    _write_bytes(sys.stderr, b"\n")
            if outcome.error and (outcome.result is None or outcome.error != outcome.result.error):
                _write_bytes(sys.stderr, f"[{outcome.id}] {outcome.error}\n".encode())
    sys.stdout.flush()
    sys.stderr.flush()
    return result.exit_code


def _emit_transfer(result: TransferResult, as_json: bool) -> int:
    if as_json:
        json.dump(result.to_dict(), sys.stdout)
        sys.stdout.write("\n")
    else:
        resumed = f", resumed at {result.resumed_from} bytes" if result.resumed_from else ""
        sys.stdout.write(
            f"{result.direction} complete: {result.size} bytes, sha256={result.sha256}{resumed}\n"
        )
    sys.stdout.flush()
    return 0


def _progress_reporter(label: str):
    last_percent = -1

    def report(done: int, total: int) -> None:
        nonlocal last_percent
        percent = 100 if total == 0 else int(done * 100 / total)
        if percent == last_percent or (percent not in (0, 100) and percent // 10 == last_percent // 10):
            return
        last_percent = percent
        print(f"{label}: {done}/{total} bytes ({percent}%)", file=sys.stderr)

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="privy",
        description=(
            "Remote Python, Bash, and PowerShell execution, file transfer, "
            "and HTTP proxying over Azure Relay."
        ),
        epilog=f"""Execution behavior:
  --timeout-s applies to Python, Bash, and PowerShell. Requests with a timeout above
  {RELAY_RESPONSE_LIMIT_S:g}s automatically use submit + long-poll so they can outlive Azure
  Relay's response deadline; the CLI still waits for and emits the final result.
  --async-job forces that path and --no-async-job disables it. The Python SDK
  additionally exposes RelayClient.submit(), poll(), and cancel().

Credential behavior:
  Every Relay command accepts either PRIVY_RELAY_TOKEN/--token, or the
  PRIVY_RELAY_KEYRULE + PRIVY_RELAY_KEY pair. Injected tokens are checked for
  expiry and audience before dialing. privy token mint creates short-lived
  consumer tokens from role-specific signing credentials.

Examples:
  privy server --token "$PRIVY_RELAY_TOKEN"
  privy client --bash "uname -a" --timeout-s 30
  privy client --powershell "Get-ComputerInfo" --timeout-s 30
  privy client --batch commands.json --json
  privy file upload ./model.pkl /tmp/model.pkl --overwrite
  privy token mint --rights send --ttl 30m
""",
    )
    parser.add_argument("--version", action="version", version=f"privy {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase log verbosity (-v for INFO, -vv for DEBUG)",
    )
    sub = parser.add_subparsers(
        dest="command",
        metavar="{server,client,proxy,file,token}",
        title="commands",
    )

    server = sub.add_parser(
        "server",
        help="run the Azure Relay listener",
        description="Run the listener and execute incoming jobs, transfers, and proxy requests.",
    )
    _add_relay_args(server)
    server.add_argument("--max-workers", type=_positive_int, default=32, help="worker threads")
    server.add_argument(
        "--recv-timeout-s",
        type=_positive_float,
        default=1.0,
        help="websocket receive timeout in seconds",
    )
    server.add_argument(
        "--proxy-target",
        default=None,
        metavar="URL",
        help="forward proxied HTTP to this local URL, e.g. http://127.0.0.1:8080",
    )
    server.add_argument(
        "--credential-fd",
        dest="_credential_fd",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    server.set_defaults(func=_cmd_server)

    client = sub.add_parser(
        "client",
        help="run one command or a dependency graph remotely",
        description=f"""Run one command or a JSON dependency graph.

Single-command timeout values above {RELAY_RESPONSE_LIMIT_S:g}s automatically use the async
submit/long-poll transport while this CLI waits for the final response. A batch manifest owns
each command's mode/timeout and may set max_parallel (default: 32).""",
        epilog="""Batch manifest:
  {
    "max_parallel": 32,
    "commands": [
      {"id": "extract", "kind": "bash", "code": "./extract.sh"},
      {"id": "inspect", "kind": "powershell", "code": "Get-ComputerInfo"},
      {"id": "load", "kind": "python", "code": "load()", "mode": "inprocess",
       "timeout_s": 1200, "depends_on": ["extract"]}
    ]
  }
""",
    )
    _add_relay_args(client)
    code = client.add_mutually_exclusive_group(required=True)
    code.add_argument("--bash", metavar="CODE", help="Bash code to execute remotely")
    code.add_argument("--powershell", metavar="CODE", help="PowerShell code to execute remotely")
    code.add_argument("--python", metavar="CODE", help="Python code to execute remotely")
    code.add_argument("--file", metavar="PATH", help="read code from PATH ('-' for stdin)")
    code.add_argument("--batch", metavar="PATH", help="read a JSON command DAG from PATH ('-' for stdin)")
    client.add_argument(
        "--file-kind",
        choices=("python", "bash", "powershell"),
        default="python",
        help="how to interpret --file",
    )
    client.add_argument(
        "--mode",
        choices=("subprocess", "inprocess"),
        default="subprocess",
        help="remote execution mode; inprocess is Python-only",
    )
    client.add_argument(
        "--timeout-s",
        type=_positive_float,
        default=DEFAULT_TIMEOUT_S,
        help=(
            "remote execution timeout for --bash/--powershell/--python/--file; values above "
            f"{RELAY_RESPONSE_LIMIT_S:g}s automatically use submit + long-poll"
        ),
    )
    job = client.add_mutually_exclusive_group()
    job.add_argument(
        "--async-job",
        dest="async_job",
        action="store_true",
        default=None,
        help="force submit + long-poll while still waiting for the final result",
    )
    job.add_argument(
        "--no-async-job",
        dest="async_job",
        action="store_false",
        help="force one Relay round-trip; long commands may hit Relay's response deadline",
    )
    client.add_argument("--json", action="store_true", help="emit the complete result as JSON")
    client.set_defaults(func=_cmd_client)

    proxy = sub.add_parser(
        "proxy",
        help="expose a remote HTTP service on a local port",
        description="Forward local browser HTTP requests through the Relay listener.",
    )
    _add_relay_args(proxy)
    proxy.add_argument("--local-port", type=_positive_int, default=3000, help="local listen port")
    proxy.set_defaults(func=_cmd_proxy)

    file_parser = sub.add_parser(
        "file",
        help="upload or download one resumable file",
        description=(
            "Transfer one file in resumable SHA-256-verified chunks. Destinations are never "
            "replaced unless --overwrite is explicit."
        ),
    )
    file_parser.set_defaults(_help_parser=file_parser)
    file_sub = file_parser.add_subparsers(
        dest="file_command",
        metavar="{upload,download}",
        title="file commands",
    )

    upload = file_sub.add_parser("upload", help="copy a local file to the listener")
    _add_relay_args(upload)
    upload.add_argument("local", metavar="LOCAL", help="local source file")
    upload.add_argument("remote", metavar="REMOTE", help="remote destination path")
    _add_transfer_args(upload)
    upload.set_defaults(func=_cmd_file_upload)

    download = file_sub.add_parser("download", help="copy a listener file to this client")
    _add_relay_args(download)
    download.add_argument("remote", metavar="REMOTE", help="remote source file")
    download.add_argument("local", metavar="LOCAL", help="local destination path")
    _add_transfer_args(download)
    download.set_defaults(func=_cmd_file_download)

    token_parser = sub.add_parser(
        "token",
        help="mint short-lived Relay SAS tokens",
        description=(
            "Broker-side token operations. Azure rights come from the selected authorization "
            "rule; --rights chooses its role-specific key environment."
        ),
    )
    token_parser.set_defaults(_help_parser=token_parser)
    token_sub = token_parser.add_subparsers(
        dest="token_command",
        metavar="{mint}",
        title="token commands",
    )
    mint = token_sub.add_parser(
        "mint",
        help="mint a token from a Send-only or Listen-only rule",
        description="Write one SAS token to stdout without starting a listener.",
    )
    _add_common_args(mint)
    for dest, env in _BASE_RELAY_SETTINGS:
        mint.add_argument(
            f"--{dest}",
            default=None,
            metavar=dest.upper(),
            help=f"Azure Relay {dest} (env: {env})",
        )
    mint.add_argument(
        "--rights",
        choices=("send", "listen"),
        required=True,
        help=(
            "select PRIVY_RELAY_SEND_KEYRULE/KEY or PRIVY_RELAY_LISTEN_KEYRULE/KEY; "
            "the Azure rule enforces the actual rights"
        ),
    )
    mint.add_argument("--keyrule", default=None, metavar="NAME", help="override selected role keyrule")
    mint.add_argument("--key", default=None, metavar="KEY", help="override selected role signing key")
    mint.add_argument(
        "--ttl",
        type=_parse_duration,
        default=30 * 60,
        metavar="DURATION",
        help="token lifetime using s/m/h/d suffixes",
    )
    mint.set_defaults(func=_cmd_token_mint)

    return parser


def _add_transfer_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--chunk-size",
        type=_parse_size,
        default=DEFAULT_CHUNK_SIZE,
        metavar="BYTES",
        help="raw bytes per Relay request; accepts KiB/MiB suffixes",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing destination after checksum verification",
    )
    parser.add_argument("--json", action="store_true", help="emit transfer metadata as JSON")


def _cmd_server(args: argparse.Namespace) -> int:
    _guard_packaged_server(args)
    if args._credential_fd is None:
        relay = _resolve_relay(args)
        if _server_reexec_required():
            _reexec_server(relay, args)
        max_workers = args.max_workers
        recv_timeout_s = args.recv_timeout_s
        proxy_target = args.proxy_target
    else:
        relay, max_workers, recv_timeout_s, proxy_target = _read_server_config(args._credential_fd)
    for name in _SERVER_SECRET_ENV_VARS:
        os.environ.pop(name, None)
    _harden_server_process()
    server = RelayServer(
        **relay,
        max_workers=max_workers,
        recv_timeout_s=recv_timeout_s,
        proxy_target=proxy_target,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0


def _cmd_client(args: argparse.Namespace) -> int:
    client = RelayClient(**_resolve_relay(args))
    if args.batch is not None:
        if args.async_job is not None:
            raise CliError("--async-job/--no-async-job apply to single commands, not --batch")
        manifest = parse_batch_manifest(_read_text(args.batch, purpose="batch manifest"))
        return _emit_batch(
            client.run_many(manifest.commands, max_parallel=manifest.max_parallel),
            args.json,
        )

    kind, code = _read_code(args)
    if args.mode == "inprocess" and kind != "python":
        raise CliError("--mode inprocess is only valid for Python code")
    if kind == "bash":
        result = client.run_bash(code, timeout_s=args.timeout_s, async_job=args.async_job)
    elif kind == "powershell":
        result = client.run_powershell(code, timeout_s=args.timeout_s, async_job=args.async_job)
    else:
        result = client.run_python(
            code,
            mode=args.mode,
            timeout_s=args.timeout_s,
            async_job=args.async_job,
        )
    return _emit(result, args.json)


def _cmd_proxy(args: argparse.Namespace) -> int:
    proxy = ProxyClientServer(**_resolve_relay(args), local_port=args.local_port)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        proxy.stop()
    return 0


def _cmd_file_upload(args: argparse.Namespace) -> int:
    client = RelayClient(**_resolve_relay(args))
    result = client.upload_file(
        args.local,
        args.remote,
        chunk_size=args.chunk_size,
        overwrite=args.overwrite,
        progress=_progress_reporter("upload"),
    )
    return _emit_transfer(result, args.json)


def _cmd_file_download(args: argparse.Namespace) -> int:
    client = RelayClient(**_resolve_relay(args))
    result = client.download_file(
        args.remote,
        args.local,
        chunk_size=args.chunk_size,
        overwrite=args.overwrite,
        progress=_progress_reporter("download"),
    )
    return _emit_transfer(result, args.json)


def _cmd_token_mint(args: argparse.Namespace) -> int:
    namespace, path, keyrule, key = _resolve_mint_args(args)
    token = create_sas_token(namespace, path, keyrule, key, ttl_seconds=args.ttl)
    sys.stdout.write(token + "\n")
    sys.stdout.flush()
    return 0


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if isinstance(value, bool) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parse_duration(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhdSMHD]?)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError("must be an integer with an optional s, m, h, or d suffix")
    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
    seconds = amount * multiplier
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return seconds


def _parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*(b|kib|mib)?\s*", value, flags=re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError("must be bytes or use a KiB/MiB suffix")
    suffix = (match.group(2) or "").lower()
    multiplier = {"": 1, "b": 1, "kib": 1024, "mib": 1024 * 1024}[suffix]
    return _positive_int(int(match.group(1)) * multiplier)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None) or not hasattr(args, "func"):
        help_parser = getattr(args, "_help_parser", parser)
        help_parser.print_help()
        return 2

    _configure_logging(max(args.verbose, getattr(args, "verbose_sub", 0)))

    try:
        return args.func(args)
    except RelayTokenExpiredError as exc:
        print(f"privy: {exc}", file=sys.stderr)
        return TOKEN_EXPIRED_EXIT_CODE
    except (CliError, BatchValidationError, RelayTokenError) as exc:
        print(f"privy: error: {exc}", file=sys.stderr)
        return 2
    except TransferError as exc:
        print(f"privy: transfer failed [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    for handler in root.handlers:
        if not any(isinstance(item, _RelayRedactionFilter) for item in handler.filters):
            handler.addFilter(_RelayRedactionFilter())
    logging.getLogger("privy").setLevel(level)
    for name in ("requests", "urllib3", "websocket"):
        logging.getLogger(name).setLevel(logging.WARNING)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
