from privy.hub import render
from privy.hub.guide import short_names


def _job(**fields):
    job = {
        "job_id": "abc123",
        "target": "dev/box1_windows",
        "language": "bash",
        "state": "succeeded",
        "done": True,
    }
    job.update(fields)
    return job


def test_jobs_render_like_a_terminal():
    text = render.render_job(_job(exit_code=0, duration_ms=1234, stdout="hi\n", stderr="warn"))

    assert (
        text == "job abc123 on dev/box1_windows (bash): succeeded, exit 0, 1.2s\nhi\n--- stderr ---\nwarn\n"
    )


def test_running_jobs_say_how_to_continue_for_each_transport():
    running = _job(state="running", done=False, stdout="", stderr="", stdout_skipped_bytes=10)

    rest = render.render_job(running)
    mcp = render.render_job(running, transport="mcp")

    assert "[10 earlier bytes of stdout skipped; page them with read_job_output]" in rest
    assert "GET /v1/jobs/abc123?format=text" in rest and "/v1/jobs/abc123/stream" in rest
    assert 'call get_job(job_id="abc123")' in mcp
    assert render.render_job(_job(stdout="", stderr="")).endswith("(no new output)\n")


def test_errors_and_transfers_render_their_details():
    failed = render.render_job(
        _job(state="error", exit_code=None, error="offline: no listener", stdout="", stderr="")
    )
    assert failed.startswith("job abc123 on dev/box1_windows (bash): error (offline: no listener)\n")

    transfer = render.render_job(
        {
            "job_id": "t1",
            "target": "dev/box1_windows",
            "type": "upload",
            "state": "succeeded",
            "done": True,
            "local_path": "C:/a",
            "remote_path": "/a",
            "result": {"size": 3, "sha256": "f" * 64},
        }
    )
    assert "(upload): succeeded" in transfer and "3 bytes, sha256 " + "f" * 64 in transfer

    error = render.render_error(
        {"code": "target_not_found", "message": "unknown", "details": {"candidates": ["a", "b"]}}
    )
    assert error == "error (target_not_found): unknown\ncandidates: a, b\n"


def test_broadcasts_list_each_machine():
    text = render.render_broadcast(
        {
            "broadcast_id": "b1",
            "patterns": ["dev/*"],
            "done": False,
            "summary": {"running": 1, "succeeded": 1, "skipped": 1},
            "jobs": [
                _job(exit_code=0, duration_ms=10, stdout="one\n", stderr=""),
                _job(
                    job_id="def456",
                    target="dev/box2_windows",
                    state="running",
                    done=False,
                    stdout="",
                    stderr="",
                ),
            ],
            "skipped": [{"target": "dev/box3_windows", "status": "offline"}],
        },
        transport="mcp",
    )

    assert text.startswith("broadcast b1 (dev/*): 1 running, 1 succeeded, 1 skipped\n")
    assert "== dev/box1_windows (bash): succeeded, exit 0, 0.0s\none\n" in text
    assert "== dev/box2_windows (bash): running\n(no new output)\n" in text
    assert "skipped dev/box3_windows (offline)" in text
    assert text.endswith('call get_broadcast(broadcast_id="b1") for the next output\n')


def test_output_pages_and_unknown_operations():
    page = render.render_output(
        {"stream": "stdout", "offset": 0, "next_offset": 3, "total_bytes": 9, "data": "abc", "eof": False}
    )
    assert page == "abc\n[stdout bytes 0-3 of 9]\n"
    assert render.render("something_else", {"a": 1}) == '{\n  "a": 1\n}\n'


def test_short_names_prefer_machine_then_hybrid_connection_then_id():
    class Target:
        def __init__(self, relay, path):
            self.relay, self.path, self.id = relay, path, f"{relay}/{path}"

    targets = [
        Target("dev", "box1_windows"),
        Target("lab", "box1_windows"),
        Target("dev", "gpu_linux"),
        Target("dev", "gpu_windows"),
        Target("dev", "solo_wsl"),
    ]

    assert short_names(targets) == {
        "dev/box1_windows": "dev/box1_windows",
        "lab/box1_windows": "lab/box1_windows",
        "dev/gpu_linux": "gpu_linux",
        "dev/gpu_windows": "gpu_windows",
        "dev/solo_wsl": "solo",
    }
