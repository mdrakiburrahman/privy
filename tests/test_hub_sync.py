"""Sync the real generated helper and transfer handler against isolated local directories."""

import ast
import json
import os
import subprocess
import sys
import threading

import pytest
import requests
from hub_fakes import Hub, exec_result, write_relays

from privy.hub import sync as sync_module
from privy.hub.app import build_hub
from privy.hub.sync import sync, validate_source
from privy.transfer import TransferError, handle_transfer_request, upload_file


class LocalClient:
    def __init__(self):
        self.uploads = []
        self.commands = []

    def remote(self, code):
        self.commands.append(code)
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=20, check=True
        )
        return result.stdout

    def send(self, request, *, on_submit, **kwargs):
        on_submit("sync-helper")
        return exec_result(stdout=self.remote(request.code).encode())

    def cancel(self, request, remote_job_id):
        return exec_result()

    def upload_file(self, source, destination, **kwargs):
        self.uploads.append(destination)
        return upload_file(lambda raw: handle_transfer_request(raw).to_json(), source, destination, **kwargs)


def run_sync(source, destination, destination_type="directory", *, client=None, **kwargs):
    client = client or LocalClient()
    updates = []
    result = sync(
        client,
        str(source),
        str(destination),
        destination_type=destination_type,
        overwrite=kwargs.pop("overwrite", True),
        check_cancel=kwargs.pop("check_cancel", lambda: None),
        report=lambda update: updates.append(dict(update)),
        run_remote=kwargs.pop("run_remote", client.remote),
        **kwargs,
    )
    return result, client, updates


def test_sync_directory_contents_creates_empty_dirs_and_preserves_extras(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "empty").mkdir()
    (source / "nested" / "message.txt").write_text("hello", encoding="utf-8")
    (source / ".hidden").write_bytes(b"hidden")
    (source / "zero").touch()
    destination = tmp_path / "remote"
    destination.mkdir()
    (destination / "keep.txt").write_bytes(b"keep")
    result, client, updates = run_sync(source, destination)
    assert (destination / "nested" / "empty").is_dir()
    assert (destination / "nested" / "message.txt").read_bytes() == b"hello"
    assert (destination / ".hidden").read_bytes() == b"hidden"
    assert (destination / "zero").read_bytes() == b""
    assert (destination / "keep.txt").read_bytes() == b"keep"
    assert not (destination / "source").exists()
    assert result["files_copied"] == result["files_total"] == result["files_done"] == 3
    assert result["directories_total"] == 3
    assert result["bytes_done"] == result["bytes_total"] == result["bytes_copied"] == 11
    assert len(client.uploads) == 3 and len(client.commands) == 1
    assert updates[-1]["phase"] == "completed"


def test_sync_skips_identical_and_replaces_changed_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "same").write_bytes(b"same")
    (source / "changed").write_bytes(b"new")
    destination = tmp_path / "remote"
    destination.mkdir()
    (destination / "same").write_bytes(b"same")
    (destination / "changed").write_bytes(b"old")
    before = (destination / "same").stat().st_mtime_ns
    result, client, _ = run_sync(source, destination)
    assert result["files_copied"] == 1 and result["files_skipped"] == 1
    assert result["bytes_done"] == 7 and result["bytes_copied"] == 3
    assert client.uploads == [str(destination / "changed")]
    assert (destination / "same").stat().st_mtime_ns == before
    assert (destination / "changed").read_bytes() == b"new"


@pytest.mark.parametrize("destination_type", ["directory", "file"])
def test_sync_single_file_has_explicit_destination_semantics(tmp_path, destination_type):
    source = tmp_path / "name with ' quotes \u00e9.txt"
    source.write_bytes(b"payload")
    destination = tmp_path / "new" / "target"
    result, _, _ = run_sync(source, destination, destination_type)
    expected = destination / source.name if destination_type == "directory" else destination
    assert expected.read_bytes() == b"payload"
    assert result["files_copied"] == 1 and result["bytes_total"] == 7


def test_sync_empty_directory_is_successful(tmp_path):
    source = tmp_path / "empty"
    source.mkdir()
    result, client, _ = run_sync(source, tmp_path / "remote")
    assert result["files_total"] == result["bytes_total"] == 0
    assert result["directories_total"] == 1 and client.uploads == []
    assert (tmp_path / "remote").is_dir()


def test_sync_overwrite_false_skips_identical_but_rejects_conflicts_before_writes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"new")
    (source / "b").write_bytes(b"same")
    destination = tmp_path / "remote"
    destination.mkdir()
    (destination / "a").write_bytes(b"old")
    (destination / "b").write_bytes(b"same")
    client = LocalClient()
    with pytest.raises(TransferError, match="overwrite"):
        run_sync(source, destination, overwrite=False, client=client)
    assert client.uploads == [] and (destination / "a").read_bytes() == b"old"
    (destination / "a").write_bytes(b"new")
    result, _, _ = run_sync(source, destination, overwrite=False)
    assert result["files_skipped"] == 2 and result["files_copied"] == 0


@pytest.mark.parametrize("kind", ["file_over_dir", "dir_over_file"])
def test_sync_rejects_destination_type_conflicts(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    if kind == "file_over_dir":
        (source / "conflict").write_bytes(b"x")
        (destination / "conflict").mkdir()
    else:
        (source / "conflict").mkdir()
        (destination / "conflict").write_bytes(b"x")
    with pytest.raises(TransferError, match="type"):
        run_sync(source, destination)


@pytest.mark.parametrize("local_path", ["relative", "", "missing"])
def test_validate_source_rejects_relative_empty_and_missing(tmp_path, local_path):
    path = str(tmp_path / "absent") if local_path == "missing" else local_path
    with pytest.raises(TransferError):
        validate_source(path, "directory")


def test_validate_source_rejects_directory_to_exact_file(tmp_path):
    with pytest.raises(TransferError, match="directory"):
        validate_source(str(tmp_path), "file")


def test_sync_rejects_nonabsolute_remote_path(tmp_path):
    source = tmp_path / "source"
    source.touch()
    with pytest.raises(TransferError, match="absolute"):
        run_sync(source, "relative", "file")


def test_sync_rejects_symbolic_links_in_source_and_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"do not copy")
    try:
        (source / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted")
    with pytest.raises(TransferError, match="link"):
        run_sync(source, tmp_path / "destination")
    (source / "linked").unlink()
    (source / "linked").mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(TransferError, match="link"):
        run_sync(source, destination)
    assert list(outside.iterdir()) == [outside / "secret"]


def test_sync_rejects_junction_in_source_on_windows(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction test")
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = source / "linked"
    subprocess.run(
        [os.environ["ComSpec"], "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        check=True,
    )
    try:
        with pytest.raises(TransferError, match="link"):
            run_sync(source, tmp_path / "destination")
    finally:
        junction.rmdir()


def test_sync_manifest_entry_limit_is_inclusive(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "one").touch()
    monkeypatch.setattr(sync_module, "MAX_SYNC_ENTRIES", 2)
    assert run_sync(source, tmp_path / "ok")[0]["files_copied"] == 1
    (source / "two").touch()
    client = LocalClient()
    with pytest.raises(TransferError, match="limit"):
        run_sync(source, tmp_path / "too-many", client=client)
    assert client.commands == [] and not (tmp_path / "too-many").exists()


def test_sync_manifest_serialized_size_limit_is_inclusive(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.touch()
    # Capture the exact manifest payload size instead of approximating raw filename length.
    client = LocalClient()
    payloads = []

    def remote(code):
        payloads.append(ast.literal_eval(code.splitlines()[0].split(" = ", 1)[1]))
        return client.remote(code)

    run_sync(source, tmp_path / "dest", "file", run_remote=remote)
    size = len(json.dumps(payloads[0]["entries"], ensure_ascii=True).encode("utf-8"))
    monkeypatch.setattr(sync_module, "MAX_SYNC_MANIFEST_BYTES", size)
    run_sync(source, tmp_path / "exact", "file")
    monkeypatch.setattr(sync_module, "MAX_SYNC_MANIFEST_BYTES", size - 1)
    with pytest.raises(TransferError, match="limit"):
        run_sync(source, tmp_path / "over", "file")


@pytest.mark.parametrize("bad_response", ["not-json", "{}", '{"root": "/tmp", "same": [999]}'])
def test_sync_rejects_invalid_remote_metadata(tmp_path, bad_response):
    source = tmp_path / "source"
    source.touch()
    with pytest.raises(TransferError, match="response"):
        run_sync(source, tmp_path / "remote", run_remote=lambda code: bad_response)


def test_sync_cancellation_checks_hashing_and_each_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"a")
    (source / "b").write_bytes(b"b")
    client = LocalClient()

    class Cancelled(Exception):
        """Test cancellation."""

    def cancel():
        if client.uploads:
            raise Cancelled()

    with pytest.raises(Cancelled):
        run_sync(source, tmp_path / "remote", client=client, check_cancel=cancel)
    assert len(client.uploads) == 1


def test_sync_rejects_source_mutation_between_manifest_and_upload(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"before")
    client = LocalClient()

    def remote(code):
        response = client.remote(code)
        source.write_bytes(b"changed")
        return response

    with pytest.raises(TransferError, match="changed"):
        run_sync(source, tmp_path / "remote", "file", client=client, run_remote=remote)
    assert client.uploads == []


def test_sync_cancellation_interrupts_local_hash_before_remote_writes(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    checks = []
    client = LocalClient()

    def cancel():
        checks.append(True)
        if len(checks) == 3:
            raise RuntimeError("cancelled during hashing")

    with pytest.raises(RuntimeError, match="cancelled during hashing"):
        run_sync(source, tmp_path / "remote", client=client, check_cancel=cancel)
    assert client.uploads == client.commands == []


def test_sync_rejects_destination_junction_on_windows(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction test")
    source = tmp_path / "source"
    source.mkdir()
    (source / "subdir").mkdir()
    (source / "subdir" / "file").write_bytes(b"do not write through link")
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    junction = destination / "subdir"
    subprocess.run(
        [os.environ["ComSpec"], "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        check=True,
    )
    try:
        with pytest.raises(TransferError, match="link"):
            run_sync(source, destination)
        assert not (outside / "file").exists()
    finally:
        junction.rmdir()


@pytest.mark.parametrize("names", [["Name", "name"], ["CON.txt"], ["bad:stream"], ["bad."]])
def test_remote_prepare_rejects_windows_aliases_and_invalid_names(tmp_path, names):
    if os.name != "nt":
        pytest.skip("Windows filename policy")
    document = {
        "remote_path": str(tmp_path / "remote"),
        "overwrite": True,
        "entries": [{"path": name, "directory": False, "size": 0, "sha256": "0" * 64} for name in names],
    }
    result = json.loads(LocalClient().remote(f"document = {document!r}\n" + sync_module._REMOTE_PREPARE))
    assert "error" in result and not (tmp_path / "remote").exists()


def test_sync_is_async_over_real_http_with_text_status_and_no_mcp_tool(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    write_relays(home / "relays.json")
    client = LocalClient()
    runtime = build_hub(home=home, port=0, version="test", client_factory=lambda target: client)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    source = tmp_path / "source"
    source.write_bytes(b"http-file")
    try:
        args = {
            "target": "dev/box1_windows",
            "local_path": str(source),
            "remote_path": str(tmp_path / "remote" / "renamed"),
            "destination_type": "file",
        }
        response = requests.post(runtime.url + "/v1/files/sync", json=args, timeout=5)
        assert response.status_code == 202 and response.headers["Cache-Control"] == "no-store"
        status_url = runtime.url + response.headers["Location"]
        view = requests.get(status_url + "?wait_s=10", timeout=15).json()
        assert view["state"] == "succeeded" and view["result"]["files_copied"] == 1
        assert (tmp_path / "remote" / "renamed").read_bytes() == b"http-file"
        text = requests.post(runtime.url + "/v1/files/sync?format=text", json=args, timeout=5)
        assert text.status_code == 202 and text.headers["Location"].startswith("/v1/jobs/")
        terminal = requests.get(runtime.url + text.headers["Location"] + "?wait_s=10&format=text", timeout=15)
        assert "1 skipped" in terminal.text and "None bytes" not in terminal.text
        rejected = requests.post(
            runtime.url + "/v1/files/sync",
            json=args,
            headers={"Origin": "https://attacker.example"},
            timeout=5,
        )
        assert rejected.status_code == 403
    finally:
        runtime.shutdown()
        thread.join(5)


def test_rest_sync_job_reports_counts_and_renders_text(tmp_path, monkeypatch):
    hub = Hub(tmp_path)
    client = LocalClient()
    monkeypatch.setattr(hub.registry, "client", lambda target: client)
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"contents")
    destination = tmp_path / "remote"
    response = hub.call(
        "POST",
        "/v1/files/sync",
        {
            "target": "dev/box1_windows",
            "local_path": str(source),
            "remote_path": str(destination),
            "destination_type": "directory",
        },
    )
    assert response.status == 202
    job_id = response.payload["job_id"]
    assert response.headers["Location"] == f"/v1/jobs/{job_id}"
    view = hub.call("GET", f"/v1/jobs/{job_id}?wait_s=10").payload
    assert view["state"] == "succeeded" and view["type"] == "sync"
    assert view["destination_type"] == "directory"
    assert view["result"]["files_copied"] == 1 and view["progress"]["files_done"] == 1
    assert (destination / "file").read_bytes() == b"contents"
    text = hub.call("GET", f"/v1/jobs/{job_id}?format=text").text
    assert "1 copied" in text and "None bytes" not in text
    audit = [json.loads(line) for line in hub.audit_path.read_text().splitlines()]
    assert [record["event"] for record in audit] == ["job.start", "job.finish"]
    assert audit[0]["destination_type"] == "directory"
    assert audit[-1]["files_copied"] == 1
    # The exact same request now skips the file.
    response = hub.call(
        "POST",
        "/v1/files/sync",
        {
            "target": "dev/box1_windows",
            "local_path": str(source),
            "remote_path": str(destination),
            "destination_type": "directory",
        },
    )
    view = hub.call("GET", f"/v1/jobs/{response.payload['job_id']}?wait_s=10").payload
    assert view["result"]["files_skipped"] == 1 and view["result"]["bytes_copied"] == 0


@pytest.mark.parametrize("queued", [False, True])
def test_sync_job_cancellation_stops_work_and_retains_progress(tmp_path, monkeypatch, queued):
    hub = Hub(tmp_path, max_running=1)
    entered, release = threading.Event(), threading.Event()
    client = LocalClient()
    original = client.remote

    def blocking_remote(code):
        entered.set()
        assert release.wait(5)
        return original(code)

    monkeypatch.setattr(client, "remote", blocking_remote)
    monkeypatch.setattr(hub.registry, "client", lambda target: client)
    source = tmp_path / "source"
    source.write_bytes(b"contents")
    target = hub.registry.resolve("dev/box1_windows")
    if queued:
        hub.jobs._slots.acquire()
    try:
        job = hub.jobs.start_sync(
            target,
            local_path=str(source),
            remote_path=str(tmp_path / "remote"),
            destination_type="file",
            overwrite=True,
            origin="rest",
        )
        if not queued:
            assert entered.wait(5)
        hub.jobs.cancel(job.id)
        release.set()
    finally:
        release.set()
        if queued:
            hub.jobs._slots.release()
    hub.jobs.wait(job, 5)
    assert job.state == "cancelled"
    assert client.uploads == []
    if queued:
        assert client.commands == []
    else:
        assert job.view(max_output_bytes=0)["result"]["files_copied"] == 0


def test_sync_failure_retains_completed_file_counts(tmp_path, monkeypatch):
    hub = Hub(tmp_path)
    client = LocalClient()
    original = client.upload_file

    def fail_second(source, destination, **kwargs):
        if client.uploads:
            raise TransferError("test_failure", "second upload failed")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(client, "upload_file", fail_second)
    monkeypatch.setattr(hub.registry, "client", lambda target: client)
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"a")
    (source / "b").write_bytes(b"b")
    response = hub.call(
        "POST",
        "/v1/files/sync",
        {
            "target": "dev/box1_windows",
            "local_path": str(source),
            "remote_path": str(tmp_path / "remote"),
            "destination_type": "directory",
        },
    )
    view = hub.call("GET", f"/v1/jobs/{response.payload['job_id']}?wait_s=10").payload
    assert view["state"] == "failed" and "test_failure" in view["error"]
    assert view["result"]["files_copied"] == view["result"]["files_done"] == 1
