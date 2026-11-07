"""Explicit opt-in verification: two NEW processes and an isolated real SQL DB."""

from __future__ import annotations

import argparse
import base64
import os
import socket
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml
from sql_hub_process import TestConnections

from privy.hub.settings import load_settings
from privy.hub.sql_store import SqlBackend


def wait_for(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.25)
    raise AssertionError("isolated verification condition timed out")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--benchmark-jobs", type=int, default=3360)
    parser.add_argument("--peer-executable", type=Path, help="Packaged sql_hub_process fixture for hub B")
    args = parser.parse_args()
    settings = load_settings(args.artifacts, args.settings)
    assert settings and settings.database.endswith("_test"), "production SQL is forbidden"
    args.artifacts.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    prefix = f"bench-{run_id}-"
    evidence = {
        "run_id": run_id,
        "database": settings.database,
        "simulation": "test-owned in-process listeners",
        "processes": [],
        "checks": {},
        "latency_seconds": {},
    }
    processes, logs = [], []
    session = requests.Session()
    session.trust_env = False
    backend_home = args.artifacts / "controller"
    backend_home.mkdir(exist_ok=True)
    backend = SqlBackend(settings, backend_home, connect=TestConnections(settings), start_worker=False)

    def start(label):
        home = args.artifacts / f"home-{label}"
        home.mkdir(exist_ok=True)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        assert port != 7748
        listener.close()
        stdout_path = args.artifacts / f"{label}-stdout.log"
        stderr_path = args.artifacts / f"{label}-stderr.log"
        stdout, stderr = stdout_path.open("w"), stderr_path.open("w")
        logs.extend([stdout, stderr])
        command = [
            sys.executable,
            "-u",
            str(Path(__file__).with_name("sql_hub_process.py")),
            "--home",
            str(home),
            "--settings",
            str(args.settings),
            "--port",
            str(port),
        ]
        if label == "b" and args.peer_executable:
            command = [str(args.peer_executable.resolve()), *command[3:]]
        env = dict(os.environ, PYTHONPATH=str(Path.cwd() / "src"))
        started = time.perf_counter()
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env)
        processes.append(process)
        url = f"http://127.0.0.1:{port}"

        def ready():
            if process.poll() is not None:
                raise AssertionError(f"test hub {label} exited; see {stderr_path.name}")
            try:
                response = session.get(url + "/healthz", timeout=2)
                return response.status_code == 200
            except requests.ConnectionError:
                return False

        wait_for(ready, 120)
        evidence["processes"].append({"label": label, "pid": process.pid, "port": port, "command": command})
        evidence["latency_seconds"][f"startup_{label}"] = time.perf_counter() - started
        return process, url

    def call(url, method, path, body=None, expected=(200, 201, 202)):
        response = session.request(method, url + path, json=body, timeout=60)
        assert response.status_code in expected, (
            f"{method} {path}: {response.status_code} {response.text[:500]}"
        )
        return response.json()

    def completed(url, job_id):
        job = call(url, "GET", f"/v1/jobs/{job_id}?wait_s=0&stdout_offset=0&stderr_offset=0")
        return job if job["done"] else None

    try:
        a_process, a = start("a")
        _, b = start("b")
        clients = [
            call(url, "POST", "/v1/clients", {"name": f"isolated-{i}", "session_id": f"{run_id}-{i}"})
            for i, url in enumerate((a, b))
        ]
        assert (
            call(b, "POST", "/v1/clients", {"name": "same-session", "session_id": f"{run_id}-0"})["client_id"]
            == clients[0]["client_id"]
        )
        groups = call(a, "GET", "/v1/groups")
        available = next(
            g
            for g in groups
            if g["state"] == "free" and g["group"] not in {"sqltest/testbox00", "sqltest/testbox01"}
        )
        group = available["group"]
        target = available["members"]["windows"]
        evidence["race_group"] = group

        def race(i):
            response = session.post(
                (a, b)[i] + "/v1/leases",
                json={"client_id": clients[i]["client_id"], "target": group},
                timeout=60,
            )
            return response.status_code, response.json()

        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(race, (0, 1)))
        evidence["race_status_codes"] = [code for code, _ in outcomes]
        assert sorted(code for code, _ in outcomes) == [201, 409], outcomes
        lease = next(data for code, data in outcomes if code == 201)
        call(b, "POST", f"/v1/leases/{lease['lease_id']}/renew", {"client_id": lease["client_id"]})
        call(a, "DELETE", f"/v1/leases/{lease['lease_id']}?client_id={lease['client_id']}")
        evidence["checks"]["race_renew_release"] = "passed against real Azure SQL"

        lease = call(a, "POST", "/v1/leases", {"client_id": clients[0]["client_id"], "target": group})
        job = call(
            a,
            "POST",
            "/v1/jobs",
            {
                "target": target,
                "code": "block",
                "language": "python",
                "wait_s": 0,
                "lease_id": lease["lease_id"],
            },
        )
        job_id = job["job_id"]
        output = wait_for(lambda: call(b, "GET", f"/v1/jobs/{job_id}/output?offset=0&limit=4096").get("data"))
        assert "simulated: block" in output
        call(b, "POST", f"/v1/jobs/{job_id}/cancel")
        result = wait_for(lambda: completed(b, job_id))
        assert result["state"] == "cancelled"
        wait_for(lambda: backend.get("job", job_id)["state"] == "cancelled")
        with backend.transaction() as cursor:
            cursor.execute("SELECT acknowledged FROM dbo.privy_cancels WHERE job_id=?", job_id)
            assert cursor.fetchone()[0] is not None
        call(b, "DELETE", f"/v1/leases/{lease['lease_id']}?client_id={lease['client_id']}")
        evidence["checks"]["other_hub_output_and_cancel"] = (
            "passed, durable cancellation acknowledged by owner"
        )

        call(a, "POST", "/test/sql-outage", {"enabled": True})
        outage_id = f"outage-{run_id}"
        call(a, "POST", "/test/audit", {"id": outage_id})
        assert (
            session.post(a + "/v1/clients", json={"name": "offline-registration"}, timeout=10).status_code
            == 503
        )
        assert session.get(b + "/healthz", timeout=10).status_code == 200
        assert backend.get("audit", outage_id) is None
        call(a, "POST", "/test/sql-outage", {"enabled": False})
        wait_for(lambda: backend.get("audit", outage_id))
        evidence["checks"]["isolated_outage_replay"] = "passed; no file fallback, second hub stayed healthy"

        a_process.terminate()
        a_process.wait(timeout=10)
        _, restarted = start("a-restarted")
        assert completed(restarted, job_id)["state"] == "cancelled"
        assert (
            "simulated: block"
            in call(restarted, "GET", f"/v1/jobs/{job_id}/output?offset=0&limit=4096")["data"]
        )
        evidence["checks"]["restart_persistence"] = "passed with a fresh process incarnation"
        (args.artifacts / "evidence.yaml").write_text(
            yaml.safe_dump(evidence, sort_keys=False), encoding="utf-8"
        )

        now = time.time()
        encoded = base64.b64encode(b"x" * 4096).decode()
        seed_start = time.perf_counter()
        for i in range(args.benchmark_jobs):
            key = f"{prefix}{i:05d}"
            at = now - (i % 168) * 3600 - 1
            backend.enqueue(
                "output",
                key + ":00000000000000000000",
                {
                    "job_id": key,
                    "owner": backend.owner,
                    "index": 0,
                    "stream": "stdout",
                    "start": 0,
                    "end": 4096,
                    "data": encoded,
                    "created_at": at,
                },
            )
            backend.enqueue(
                "job",
                key,
                {
                    "job_id": key,
                    "owner": backend.owner,
                    "created_at": at,
                    "target": f"sqltest/testbox{i % 20:02d}_windows",
                    "type": "exec",
                    "spec": {
                        "language": "python",
                        "code": "benchmark",
                        "mode": "subprocess",
                        "timeout_s": 60,
                    },
                    "origin": "test",
                    "state": "succeeded",
                    "exit_code": 0,
                    "error": None,
                    "purpose": "Benchmark seven-day loading",
                    "lease_id": None,
                    "client_id": None,
                    "broadcast_id": None,
                    "started_at": at,
                    "finished_at": at + 1,
                    "stdout_bytes": 4096,
                    "stderr_bytes": 0,
                    "chunk_count": 1,
                },
            )
        assert backend.flush(timeout=240)
        evidence["latency_seconds"]["seed"] = time.perf_counter() - seed_start
        evidence["workload"] = {
            "devboxes": 20,
            "days": 7,
            "jobs": args.benchmark_jobs,
            "output_bytes": args.benchmark_jobs * 4096,
        }
        for label, path in (
            ("job_page", "/v1/jobs?limit=200"),
            ("dashboard_7d", "/v1/stats?range=7d"),
            ("output_page", f"/v1/jobs/{prefix}00019/output?offset=0&limit=4096"),
            ("dashboard_html", "/ui/hycos?range=7d"),
        ):
            start_time = time.perf_counter()
            response = session.get(b + path, timeout=120)
            assert response.status_code == 200, response.text[:500]
            evidence["latency_seconds"][label] = time.perf_counter() - start_time
            evidence["checks"][label] = {"status": 200, "response_bytes": len(response.content)}
            budget = 10 if label in ("dashboard_7d", "dashboard_html") else 5
            assert evidence["latency_seconds"][label] < budget, (
                f"{label} exceeded {budget}s responsiveness budget"
            )

        old = f"{prefix}expired"
        running = f"{prefix}running"
        for key, state in ((old, "succeeded"), (running, "running")):
            row = backend.get("job", f"{prefix}00000")
            row.update(job_id=key, state=state, created_at=now - 8 * 86400)
            backend.put("job", key, row)
        backend.collect()
        assert backend.get("job", old) is None and backend.get("job", running) is not None
        evidence["checks"]["bounded_gc_protects_live_jobs"] = "passed against real SQL"
        evidence["result"] = "passed"
    except BaseException as exc:
        evidence["result"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        for stream in logs:
            stream.close()
        # Only records with this run's unique benchmark prefix are removed.
        try:
            with backend.transaction() as cursor:
                cursor.execute("SELECT COUNT(*) FROM dbo.privy_output WHERE job_id LIKE ?", prefix + "%")
                output_count = cursor.fetchone()[0]
            for _ in range(output_count // 100 + 1):
                with backend.transaction() as cursor:
                    cursor.execute("DELETE TOP(100) dbo.privy_output WHERE job_id LIKE ?", prefix + "%")
            for _ in range(args.benchmark_jobs // 100 + 2):
                with backend.transaction() as cursor:
                    cursor.execute(
                        "DELETE TOP(100) dbo.privy_records WHERE kind='job' AND id LIKE ?", prefix + "%"
                    )
            evidence["cleanup"] = "test-owned processes stopped and bounded benchmark records removed"
        except Exception as cleanup_error:
            evidence["cleanup"] = f"failed: {type(cleanup_error).__name__}"
            evidence["result"] = "failed"
        finally:
            evidence["outbox_at_shutdown"] = backend.status()
            backend.close()
            session.close()
            (args.artifacts / "evidence.yaml").write_text(
                yaml.safe_dump(evidence, sort_keys=False), encoding="utf-8"
            )
        print(yaml.safe_dump(evidence, sort_keys=False))


if __name__ == "__main__":
    main()
