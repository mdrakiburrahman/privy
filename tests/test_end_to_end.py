"""End-to-end tests — real Azure Relay.

Requires ``PRIVY_RELAY_{NAMESPACE,PATH,KEYRULE,KEY}`` in the environment (see
``.env.template``). Skipped automatically otherwise.

These tests simulate the whole "Fabric notebook" loop locally: we spin a
``RelayServer`` in a background thread, send it real bash + python requests
through Azure Relay via a ``RelayClient``, and assert on stdout/stderr/exit.
"""

from __future__ import annotations

import sys
import time
import uuid

from privy import RELAY_RESPONSE_LIMIT_S, RelayClient


def test_bash_echo(relay_client: RelayClient):
    token = uuid.uuid4().hex
    r = relay_client.run_bash(f"echo {token}")
    assert r.ok, f"exit={r.exit_code} stderr={r.stderr!r}"
    assert token in r.stdout


def test_python_prints_version(relay_client: RelayClient):
    r = relay_client.run_python("import sys; print(sys.version_info[0])")
    assert r.ok, f"stderr={r.stderr!r}"
    assert r.stdout.strip() == "3"


def test_python_raises_surfaces_in_stderr(relay_client: RelayClient):
    r = relay_client.run_python("raise RuntimeError('kaboom')")
    assert not r.ok
    assert "kaboom" in r.stderr


def test_bash_exit_code_nonzero(relay_client: RelayClient):
    r = relay_client.run_bash("echo before && false")
    assert r.exit_code == 1
    assert "before" in r.stdout


def test_full_pip_then_python_loop(relay_client: RelayClient, tmp_path_factory):
    """The user-story test: pip install a library, then run Python that uses it.

    Mirrors the Fabric notebook flow (``%pip install X`` then ``import X``) but
    isolates the install into a throw-away directory so we never clobber the
    test venv. Uses ``six`` (tiny, no native deps) to keep runtime small.
    """
    target = tmp_path_factory.mktemp("privy_site")
    install = relay_client.run_bash(
        f"{sys.executable} -m ensurepip --upgrade >/dev/null 2>&1 || true; "
        f"{sys.executable} -m pip install --quiet --disable-pip-version-check "
        f"--target {target} six==1.16.0",
        timeout_s=300,
    )
    assert install.ok, (
        f"pip install failed: exit={install.exit_code}\nSTDOUT:\n{install.stdout}\nSTDERR:\n{install.stderr}"
    )

    check = relay_client.run_python(
        f"import sys; sys.path.insert(0, {str(target)!r}); import six; print('six-version', six.__version__)"
    )
    assert check.ok, f"stderr={check.stderr!r}"
    assert "six-version 1.16.0" in check.stdout


def test_timeout_returns_timed_out(relay_client: RelayClient):
    r = relay_client.run_python("import time; time.sleep(5)", timeout_s=1)
    assert r.timed_out is True


def test_async_job_outlives_relay_response_limit(relay_client: RelayClient):
    """The regression this whole API exists for.

    A synchronous relay round-trip is killed by Azure at ~60s
    ("the listener did not respond in the required time", HTTP 504). The same
    work submitted as a job must complete cleanly.
    """
    sleep_s = RELAY_RESPONSE_LIMIT_S + 20
    r = relay_client.run_python(
        f"import time; time.sleep({sleep_s}); print('survived')",
        timeout_s=sleep_s + 120,
    )
    assert r.ok, f"exit={r.exit_code} stderr={r.stderr!r}"
    assert "survived" in r.stdout
    assert r.job_id, "long work should have gone through the async job path"


def test_async_job_is_opt_outable(relay_client: RelayClient):
    r = relay_client.run_python("print('sync')", timeout_s=600, async_job=False)
    assert r.ok
    assert r.stdout.strip() == "sync"
    assert r.job_id is None


def test_short_calls_stay_synchronous(relay_client: RelayClient):
    r = relay_client.run_python("print('quick')", timeout_s=10)
    assert r.ok
    assert r.job_id is None


def test_async_job_reports_errors(relay_client: RelayClient):
    r = relay_client.run_python("raise RuntimeError('async-kaboom')", timeout_s=600)
    assert not r.ok
    assert "async-kaboom" in r.stderr


def test_async_job_latency_is_small(relay_client: RelayClient):
    """Long-polling means completion is noticed almost immediately."""
    start = time.monotonic()
    r = relay_client.run_python("print('fast-async')", timeout_s=600)
    assert r.ok
    assert "fast-async" in r.stdout
    assert time.monotonic() - start < 15
