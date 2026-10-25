"""How the hub describes itself to LLMs: the quickstart at ``/`` and the
examples embedded in ``/openapi.json``.

Examples name a machine that is online right now when one is known, so an LLM
can copy them and run them unchanged.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from privy.hub import render
from privy.hub.registry import ONLINE, TargetLookupError, language_from, machine_name

if TYPE_CHECKING:
    from privy.hub.config import Target
    from privy.hub.registry import TargetRegistry
    from privy.hub.service import HubService


#: Job id used in examples.
EXAMPLE_JOB_ID = "9f3c2a1b7d4e"


@dataclass(frozen=True)
class Example:
    """A concrete machine and commands to use in examples."""

    machine: str
    target_id: str
    language: str
    code: str
    output: str
    slow_code: str


def _example(machine: str, target_id: str, language: str) -> Example:
    # No quotes inside the code: Windows PowerShell 5.1 mangles them when calling curl.exe.
    if language == "powershell":
        return Example(
            machine,
            target_id,
            language,
            "Get-Date; hostname",
            "Thursday, October 22, 2026 11:09:00 AM\nDEVBOX01\n",
            "1..10 | % { Get-Date -Format T; sleep 1 }",
        )
    return Example(
        machine,
        target_id,
        language,
        "date; hostname",
        "Thu Oct 22 11:09:00 UTC 2026\ndevbox01\n",
        "for i in $(seq 10); do date +%T; sleep 1; done",
    )


def short_names(targets: Sequence[Target]) -> dict[str, str]:
    """The shortest name that identifies each target: machine, else hybrid connection, else id."""
    machines = Counter(machine_name(target.path) for target in targets)
    paths = Counter(target.path.lower() for target in targets)
    names: dict[str, str] = {}
    for target in targets:
        if machines[machine_name(target.path)] == 1:
            names[target.id] = machine_name(target.path)
        elif paths[target.path.lower()] == 1:
            names[target.id] = target.path
        else:
            names[target.id] = target.id
    return names


def usable_name(registry: TargetRegistry, target: Target) -> str:
    """The shortest name that resolves to ``target`` right now.

    A machine name shared with offline siblings (``devbox01_wsl``) still works,
    because resolution prefers the one online target.
    """
    for candidate in (machine_name(target.path), target.path):
        try:
            if registry.resolve(candidate).id == target.id:
                return candidate
        except TargetLookupError:
            continue
    return target.id


def example_for(service: HubService, *, live: bool = False) -> Example:
    """The first online machine with a cached probe, or a placeholder.

    ``live`` names it the way it resolves right now (see :func:`usable_name`);
    otherwise by a name that is unique whatever is online.
    """
    targets = service.registry.snapshot().targets
    names = short_names(targets)
    for target in targets:
        cached = service.registry.cached(target)
        if cached is not None and cached.status == ONLINE:
            language = language_from(cached.info, target.path) or "powershell"
            name = usable_name(service.registry, target) if live else names[target.id]
            return _example(name, target.id, language)
    return _example("devbox01", "dev/devbox01_windows", "powershell")


def _quote(code: str) -> str:
    return "'" + code.replace("'", "''") + "'"


def quickstart(service: HubService, base_url: str) -> str:
    """Markdown guide served at ``/`` and ``/llms.txt``, with the machines online right now."""
    snapshot = service.registry.snapshot()
    targets = list(snapshot.targets)
    probes = service.registry.probe_many(targets)
    online = [target for target in targets if probes[target.id].status == ONLINE]
    names = {target.id: usable_name(service.registry, target) for target in online}
    example = example_for(service, live=True)
    lines = [
        "# privy hub",
        "",
        "Run PowerShell, Bash, or Python on remote machines from this computer and see the output live, as",
        "if the machine were local. Each machine runs a privy listener behind Azure Relay; this hub holds the",
        "keys. **No authentication**: call the endpoints below directly.",
        "Use a loopback URL on this port. Browsers must use this exact origin; local CLI clients need no Origin header.",
        "",
        f"Base URL: `{base_url}`",
        "",
        f"## Machines online now ({len(online)} of {len(targets)})",
        "",
    ]
    if online:
        lines += ["| name | target id | host | OS | default language |", "| --- | --- | --- | --- | --- |"]
        for target in online:
            info = probes[target.id].info or {}
            platform = info.get("platform") or {}
            lines.append(
                f"| {names[target.id]} | {target.id} | {info.get('hostname', '?')} | "
                f"{platform.get('description') or platform.get('system') or '?'} | "
                f"{language_from(info, target.path) or '?'} |"
            )
    elif not snapshot.relays:
        lines.append(f"No relays are hydrated yet: write `{snapshot.path}` (see docs/SCHEDULED_TASKS.md).")
    else:
        lines.append("No machine is online right now. `GET /v1/targets?format=text` shows why.")
    offline = len(targets) - len(online)
    if offline:
        lines += [
            "",
            f"{offline} more are not online; `GET {base_url}/v1/targets?format=text` lists all of them.",
        ]
    lines += [
        "",
        "## Run a command and stream its output (recommended)",
        "",
        "Output streams live, like a local process. The request body is the code; `target` (and optionally",
        "`language`, `timeout_s`) go in the query string.",
        "",
        "```powershell",
        f'curl.exe -sN "{base_url}/v1/jobs?stream=true&target={example.machine}" --data-binary {_quote(example.code)}',
        "```",
        "",
        "```text",
        f"[privy] job={EXAMPLE_JOB_ID} target={example.target_id} language={example.language}",
        *example.output.splitlines(),
        f"[privy] job={EXAMPLE_JOB_ID} state=succeeded exit=0 duration=1.4s",
        "```",
        "",
        "- The first line names the job; the last line reports the state and exit code.",
        "- For multi-line scripts or code with quotes, save it to a file and send "
        "`--data-binary '@script.ps1'`. Keep the quotes: in PowerShell a bare `@` is the splatting operator.",
        "- Disconnecting does not stop the job. Reattach with "
        f"`curl.exe -sN {base_url}/v1/jobs/<job_id>/stream`; stop it with "
        f"`curl.exe -s -X POST {base_url}/v1/jobs/<job_id>/cancel`.",
        "- A ten-second example to see streaming: "
        f'`curl.exe -sN "{base_url}/v1/jobs?stream=true&target={example.machine}" '
        f"--data-binary {_quote(example.slow_code)}`",
        "",
        "## Run without streaming",
        "",
        "```powershell",
        f'curl.exe -s "{base_url}/v1/jobs?format=text&target={example.machine}" --data-binary {_quote(example.code)}',
        "```",
        "",
        "This returns within `wait_s` (default 30) with the output so far. While the job is still running,",
        f"`curl.exe -s {base_url}/v1/jobs/<job_id>?format=text` waits again and returns only output produced",
        "since the previous call. Drop `format=text` to get JSON.",
        "",
        "PowerShell without curl:",
        "",
        "```powershell",
        f'Invoke-RestMethod -Method Post "{base_url}/v1/jobs?format=text&target={example.machine}" '
        f"-Body {_quote(example.code)}",
        "```",
        "",
        "## Everything else",
        "",
        "| Task | Request |",
        "| --- | --- |",
        f"| Export Relay credentials (sensitive, REST-only JSON) | `GET {base_url}/v1/relays` |",
        f"| List machines | `GET {base_url}/v1/targets?format=text` |",
        f"| One machine's details | `GET {base_url}/v1/targets/{example.machine}?format=text` |",
        f"| Run on many machines | `POST {base_url}/v1/broadcasts?format=text&targets=*_windows` with the code as the body |",
        f"| Follow a job | `GET {base_url}/v1/jobs/<job_id>?format=text` or `.../stream` |",
        f"| Cancel a job | `POST {base_url}/v1/jobs/<job_id>/cancel` |",
        f"| Recent jobs | `GET {base_url}/v1/jobs?format=text` |",
        f"| Copy a file to a machine | `POST {base_url}/v1/files/upload?target=...&local_path=...&remote_path=...` |",
        f"| Copy a file back | `POST {base_url}/v1/files/download?target=...&remote_path=...&local_path=...` |",
        f"| Sync a file or folder (REST only) | `POST {base_url}/v1/files/sync` with JSON: "
        "`target`, `local_path`, `remote_path`, `destination_type` (`directory` or `file`) |",
        f"| Every endpoint, parameter, and example | `GET {base_url}/openapi.json` |",
        f"| Usage metrics per HYCO (JSON) | `GET {base_url}/v1/stats?range=24h` |",
        f"| Dashboard for people (HTML: usage, jobs, logs) | `{base_url}/ui` |",
        "",
        "## Rules of thumb",
        "",
        f"- Name a machine as in the table (`{example.machine}`), or by its hybrid connection, full id",
        "  (`relay/hybrid-connection`), or host name. A name shared by several targets means the one",
        "  that is online.",
        "- `language` is optional: PowerShell on Windows machines, Bash elsewhere; pass `language=python`",
        "  for Python.",
        "- `timeout_s` defaults to 600 seconds; raise it for long jobs. Local paths in file transfers are",
        "  absolute paths on this computer.",
        "- Commands run as the listener's user with its full privileges. Prefer read-only commands unless",
        "  you were asked to change something.",
        "- MCP clients can use job/target operations as tools at `/mcp`; sync and credential export are REST-only.",
        "",
    ]
    return "\n".join(lines)


def _job_example(example: Example, *, done: bool) -> dict[str, Any]:
    output = example.output if done else example.output.splitlines(keepends=True)[0]
    job: dict[str, Any] = {
        "job_id": EXAMPLE_JOB_ID,
        "target": example.target_id,
        "language": example.language,
        "state": "succeeded" if done else "running",
        "done": done,
    }
    if done:
        job.update(exit_code=0, duration_ms=1412)
    else:
        job["duration_ms"] = 900
    job.update(stdout=output, stdout_offset=len(output.encode()), stderr="", stderr_offset=0)
    if not done:
        job["next_step"] = (
            f"Still running. Call get_job with job_id '{EXAMPLE_JOB_ID}' (REST: GET /v1/jobs/{EXAMPLE_JOB_ID}) "
            "for the next output; each call returns only output produced since the previous one."
        )
    return job


def _targets_example(example: Example) -> dict[str, Any]:
    return {
        "counts": {"targets": 2, "offline": 1, "online": 1},
        "targets": [
            {
                "id": example.target_id,
                "machine": example.machine,
                "status": "online",
                "host": "DEVBOX01",
                "os": "Windows-11-10.0.26100-SP0" if example.language == "powershell" else "Linux-6.8",
                "user": "alice",
                "languages": ["python", "bash", "powershell"],
                "privy_version": "0.3.0",
                "running_jobs": 0,
                "default_language": example.language,
                "latency_ms": 412,
            },
            {
                "id": "dev/devbox02_wsl",
                "machine": "devbox02",
                "status": "offline",
                "reason": "no listener is connected to this hybrid connection",
            },
        ],
        "relays": [{"name": "dev", "namespace": "contoso-relay", "targets": 2}],
    }


def examples(operation: str, base_url: str, example: Example) -> dict[str, Any]:
    """Request and response examples, plus copy-paste commands, for one operation."""
    machine = example.machine
    code = _quote(example.code)
    job = f"{base_url}/v1/jobs/{EXAMPLE_JOB_ID}"
    targets = _targets_example(example)
    finished = _job_example(example, done=True)
    running = _job_example(example, done=False)
    stream = (
        f"[privy] job={EXAMPLE_JOB_ID} target={example.target_id} language={example.language}\n"
        f"{example.output}[privy] job={EXAMPLE_JOB_ID} state=succeeded exit=0 duration=1.4s\n"
    )
    table: dict[str, dict[str, Any]] = {
        "list_targets": {
            "response": targets,
            "text": render.render_targets(targets),
            "samples": [
                f'curl.exe -s "{base_url}/v1/targets?format=text"',
                f'Invoke-RestMethod "{base_url}/v1/targets"',
            ],
        },
        "get_target": {"samples": [f'curl.exe -s "{base_url}/v1/targets/{machine}?format=text"']},
        "run": {
            "request": {"target": machine, "code": example.code},
            "raw": example.code,
            "response": finished,
            "text": render.render_job(finished),
            "samples": [
                f'curl.exe -sN "{base_url}/v1/jobs?stream=true&target={machine}" --data-binary {code}',
                f'curl.exe -s "{base_url}/v1/jobs?format=text&target={machine}" --data-binary {code}',
                f'Invoke-RestMethod -Method Post "{base_url}/v1/jobs?format=text&target={machine}" -Body {code}',
            ],
        },
        "get_job": {
            "response": running,
            "text": render.render_job(running),
            "samples": [f'curl.exe -s "{job}?format=text"', f'curl.exe -sN "{job}/stream"'],
        },
        "stream_job": {"text": stream, "samples": [f'curl.exe -sN "{job}/stream"']},
        "read_job_output": {"samples": [f'curl.exe -s "{job}/output?stream=stdout&offset=0&format=text"']},
        "cancel_job": {"samples": [f'curl.exe -s -X POST "{job}/cancel?format=text"']},
        "list_jobs": {"samples": [f'curl.exe -s "{base_url}/v1/jobs?format=text"']},
        "broadcast": {
            "request": {"targets": ["*_windows"], "code": example.code},
            "raw": example.code,
            "samples": [
                f'curl.exe -s "{base_url}/v1/broadcasts?format=text&targets=*_windows" --data-binary {code}'
            ],
        },
        "get_broadcast": {"samples": [f'curl.exe -s "{base_url}/v1/broadcasts/b1a2b3c4d5e6?format=text"']},
        "upload_file": {
            "request": {
                "target": machine,
                "local_path": "C:\\data\\model.pkl",
                "remote_path": "C:\\temp\\model.pkl",
            },
            "samples": [
                f'curl.exe -s -X POST "{base_url}/v1/files/upload?format=text&target={machine}'
                '&local_path=C:\\data\\model.pkl&remote_path=C:\\temp\\model.pkl"'
            ],
        },
        "download_file": {
            "request": {
                "target": machine,
                "remote_path": "C:\\temp\\log.txt",
                "local_path": "C:\\data\\log.txt",
            },
            "samples": [
                f'curl.exe -s -X POST "{base_url}/v1/files/download?format=text&target={machine}'
                '&remote_path=C:\\temp\\log.txt&local_path=C:\\data\\log.txt"'
            ],
        },
        "sync_files": {
            "request": {
                "target": machine,
                "local_path": "C:\\data\\project",
                "remote_path": "C:\\temp\\project",
                "destination_type": "directory",
            },
            "samples": [
                f'Invoke-RestMethod -Method Post "{base_url}/v1/files/sync" '
                '-ContentType "application/json" -Body (@{'
                f'target="{machine}"; local_path="C:\\data\\project"; '
                'remote_path="C:\\temp\\project"; destination_type="directory"} | ConvertTo-Json)'
            ],
        },
    }
    return table.get(operation, {})


def code_samples(operation: str, base_url: str, example: Example) -> list[dict[str, str]]:
    """``x-codeSamples`` entries for the OpenAPI document."""
    samples: list[dict[str, str]] = []
    for source in examples(operation, base_url, example).get("samples", []):
        if source.startswith("Invoke-RestMethod"):
            samples.append({"lang": "PowerShell", "label": "Invoke-RestMethod", "source": source})
        else:
            samples.append({"lang": "Shell", "label": "curl", "source": source})
    return samples
