"""Command-line interface for privy.

Exposed both as the ``privy`` console script (``uv run privy ...``) and as the
entrypoint of the self-contained PyInstaller binary, so a box with no Python
can still run either side of the tunnel.

Relay settings are read from ``PRIVY_RELAY_*`` environment variables and can be
overridden by explicit flags.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from privy import __version__
from privy.client import ExecResult, RelayClient
from privy.protocol import DEFAULT_TIMEOUT_S
from privy.proxy import ProxyClientServer
from privy.server import RelayServer

#: (flag dest, environment variable) for every required relay setting.
_RELAY_SETTINGS = (
    ("namespace", "PRIVY_RELAY_NAMESPACE"),
    ("path", "PRIVY_RELAY_PATH"),
    ("keyrule", "PRIVY_RELAY_KEYRULE"),
    ("key", "PRIVY_RELAY_KEY"),
)


class CliError(Exception):
    """User-facing error; printed without a traceback."""


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add ``-v`` to a subparser so it works on either side of the subcommand."""
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
    group = parser.add_argument_group("relay connection")
    for dest, env in _RELAY_SETTINGS:
        group.add_argument(
            f"--{dest}",
            default=None,
            metavar=dest.upper(),
            help=f"Azure Relay {dest} (env: {env})",
        )


def _resolve_relay(args: argparse.Namespace) -> dict[str, str]:
    """Merge flags over environment variables, erroring on anything missing."""
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for dest, env in _RELAY_SETTINGS:
        value = getattr(args, dest, None) or os.environ.get(env, "")
        if not value:
            missing.append(f"--{dest} (or {env})")
        else:
            resolved[dest] = value
    if missing:
        raise CliError("missing required relay settings: " + ", ".join(missing))
    return resolved


def _read_code(args: argparse.Namespace) -> tuple[str, str]:
    """Return ``(kind, code)`` for a client invocation."""
    if args.bash is not None:
        return "bash", args.bash
    if args.python is not None:
        return "python", args.python
    kind = "bash" if args.file_kind == "bash" else "python"
    if args.file == "-":
        return kind, sys.stdin.read()
    try:
        with open(args.file, encoding="utf-8") as fh:
            return kind, fh.read()
    except OSError as exc:
        raise CliError(f"cannot read {args.file}: {exc}") from exc


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
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        if result.error:
            sys.stderr.write(f"privy: {result.error}\n")
    sys.stdout.flush()
    sys.stderr.flush()
    if result.timed_out and result.exit_code == 0:
        return 124
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="privy",
        description="Remote Python/bash execution over Azure Relay Hybrid Connections.",
    )
    parser.add_argument("--version", action="version", version=f"privy {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase log verbosity (-v for INFO, -vv for DEBUG)",
    )
    sub = parser.add_subparsers(dest="command", metavar="{server,client,proxy}")

    server = sub.add_parser("server", help="run the listener (serve_forever)")
    _add_relay_args(server)
    server.add_argument("--max-workers", type=int, default=32, help="worker threads (default: 32)")
    server.add_argument(
        "--recv-timeout-s",
        type=float,
        default=1.0,
        help="websocket receive timeout in seconds (default: 1.0)",
    )
    server.add_argument(
        "--proxy-target",
        default=None,
        metavar="URL",
        help="forward proxied HTTP to this local URL, e.g. http://127.0.0.1:8080",
    )
    server.set_defaults(func=_cmd_server)

    client = sub.add_parser("client", help="send one execution to a remote listener")
    _add_relay_args(client)
    code = client.add_mutually_exclusive_group(required=True)
    code.add_argument("--bash", metavar="CODE", help="bash code to execute remotely")
    code.add_argument("--python", metavar="CODE", help="python code to execute remotely")
    code.add_argument("--file", metavar="PATH", help="read code from PATH ('-' for stdin)")
    client.add_argument(
        "--file-kind",
        choices=("python", "bash"),
        default="python",
        help="how to interpret --file (default: python)",
    )
    client.add_argument(
        "--mode",
        choices=("subprocess", "inprocess"),
        default="subprocess",
        help="remote execution mode; inprocess is python-only (default: subprocess)",
    )
    client.add_argument(
        "--timeout-s",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"remote execution timeout in seconds (default: {DEFAULT_TIMEOUT_S:g})",
    )
    job = client.add_mutually_exclusive_group()
    job.add_argument(
        "--async-job",
        dest="async_job",
        action="store_true",
        default=None,
        help="force the submit/poll job path",
    )
    job.add_argument(
        "--no-async-job",
        dest="async_job",
        action="store_false",
        help="force a single relay round-trip",
    )
    client.add_argument("--json", action="store_true", help="emit the full result as JSON")
    client.set_defaults(func=_cmd_client)

    proxy = sub.add_parser("proxy", help="expose a remote HTTP service on a local port")
    _add_relay_args(proxy)
    proxy.add_argument("--local-port", type=int, default=3000, help="local listen port (default: 3000)")
    proxy.set_defaults(func=_cmd_proxy)

    return parser


def _cmd_server(args: argparse.Namespace) -> int:
    server = RelayServer(
        **_resolve_relay(args),
        max_workers=args.max_workers,
        recv_timeout_s=args.recv_timeout_s,
        proxy_target=args.proxy_target,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0


def _cmd_client(args: argparse.Namespace) -> int:
    kind, code = _read_code(args)
    if args.mode == "inprocess" and kind != "python":
        raise CliError("--mode inprocess is only valid for python code")
    client = RelayClient(**_resolve_relay(args))
    if kind == "bash":
        result = client.run_bash(code, timeout_s=args.timeout_s, async_job=args.async_job)
    else:
        result = client.run_python(code, mode=args.mode, timeout_s=args.timeout_s, async_job=args.async_job)
    return _emit(result, args.json)


def _cmd_proxy(args: argparse.Namespace) -> int:
    proxy = ProxyClientServer(**_resolve_relay(args), local_port=args.local_port)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        proxy.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    verbosity = max(args.verbose, getattr(args, "verbose_sub", 0))
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        return args.func(args)
    except CliError as exc:
        print(f"privy: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
