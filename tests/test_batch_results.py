import hashlib
import json
import os
import stat
import threading

import pytest

from privy.batch import BatchManifest, BatchResult, CommandOutcome, CommandSpec, run_many
from privy.batch_results import BatchResultsError, BatchResultsWriter
from privy.client import ExecResult
from privy.protocol import ExecResponse

pytestmark = pytest.mark.skipif(os.name != "posix", reason="private results directories require POSIX")


def manifest(*ids):
    return BatchManifest(tuple(CommandSpec(id=value, kind="bash", code=value) for value in ids))


def native_result(job_id="job", stdout="full native output"):
    return ExecResult.from_response(
        ExecResponse.from_output(
            exit_code=0, stdout=stdout.encode(), stderr=b"", duration_ms=5, job_id=job_id,
        )
    )


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_metadata_safe_names_private_modes_and_complete_native_outcomes(tmp_path):
    ids = ("../../escape", "/absolute/path", r"windows\path", ".", "..", "strange\n世界\x00")
    commands = manifest(*ids)
    directory = tmp_path / "results"
    raw = b'{\r\n  "commands": []\r\n}\r\n'
    outcomes = tuple(CommandOutcome(value, "succeeded", native_result()) for value in ids)
    result = BatchResult(outcomes, 42)
    old_umask = os.umask(0)
    try:
        with BatchResultsWriter(directory, commands, source="manifest.json", raw_manifest=raw) as writer:
            metadata = read_json(directory / "batch.json")
            assert metadata["schema"] == "privy.batch.results"
            assert metadata["version"] == 1
            assert len(metadata["batch_id"]) == 32
            assert metadata["manifest"] == {
                "source": "manifest.json",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "max_parallel": 32,
            }
            assert [entry["id"] for entry in metadata["commands"]] == list(ids)
            for index, outcome in reversed(list(enumerate(outcomes))):
                writer.write_outcome(outcome)
                filename = f"command-{index + 1:06d}.json"
                assert metadata["commands"][index] == {"index": index, "id": outcome.id, "file": filename}
                assert read_json(directory / filename) == {
                    "schema": "privy.batch.command",
                    "version": 1,
                    "batch_id": metadata["batch_id"],
                    "index": index,
                    "outcome": outcome.to_dict(),
                }
            assert writer.complete(result)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in directory.iterdir())
    assert not list(directory.glob(".pending-*"))
    assert read_json(directory / "complete.json") == {
        "schema": "privy.batch.complete",
        "version": 1,
        "batch_id": metadata["batch_id"],
        "state": "complete",
        "command_count": len(ids),
        "artifact_count": len(ids),
        "missing_command_ids": [],
        "errors": [],
        "result": result.to_dict(),
    }


def test_fast_artifact_is_visible_before_slow_finishes_and_dependencies_stay_live(tmp_path):
    directory = tmp_path / "results"
    slow_started = threading.Event()
    fast_published = threading.Event()
    child_submitted = threading.Event()
    slow_finished = threading.Event()
    commands = BatchManifest(
        (
            CommandSpec(id="fast", kind="bash", code="fast"),
            CommandSpec(id="slow", kind="bash", code="slow"),
            CommandSpec(id="child", kind="bash", code="child", depends_on=("fast",)),
        ),
        max_parallel=2,
    )

    class Client:
        submitted = []

        def submit(self, request):
            self.submitted.append(request.code)
            if request.code == "child":
                assert not slow_finished.is_set()
                child_submitted.set()
            return request.code

        def poll(self, request, job_id, *, wait_s):
            if job_id == "slow":
                slow_started.set()
                assert fast_published.wait(5)
                assert read_json(directory / "command-000001.json")["outcome"]["state"] == "succeeded"
                assert not (directory / "command-000002.json").exists()
                assert not (directory / "complete.json").exists()
                assert child_submitted.wait(5)
                slow_finished.set()
            elif job_id == "fast":
                assert slow_started.wait(5)
            return "done", native_result(job_id)

    client = Client()
    caller = threading.get_ident()
    with BatchResultsWriter(directory, commands, source="-", raw_manifest="input") as writer:
        def completed(outcome):
            assert threading.get_ident() == caller
            writer.write_outcome(outcome)
            if outcome.id == "fast":
                assert not slow_finished.is_set()
                fast_published.set()

        result = run_many(client, commands.commands, max_parallel=2, on_command_complete=completed)
        assert writer.complete(result)

    assert result.ok
    assert client.submitted == ["fast", "slow", "child"]
    assert fast_published.is_set() and child_submitted.is_set() and slow_finished.is_set()


def test_publish_is_atomic_and_never_truncates_native_output(tmp_path, monkeypatch):
    directory = tmp_path / "results"
    original_link = os.link
    linked = []

    def checked_link(source, destination, **kwargs):
        assert not (directory / destination).exists()
        data = read_json(directory / source)
        assert stat.S_IMODE((directory / source).stat().st_mode) == 0o600
        original_link(source, destination, **kwargs)
        assert read_json(directory / destination) == data
        linked.append(destination)

    monkeypatch.setattr(os, "link", checked_link)
    text = "large synthetic output 世界\n" * 16_384
    outcome = CommandOutcome("one", "succeeded", native_result(stdout=text))
    with BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input") as writer:
        writer.write_outcome(outcome)
        assert writer.complete(BatchResult((outcome,), 5))
    assert linked == ["batch.json", "command-000001.json", "complete.json"]
    assert read_json(directory / "command-000001.json")["outcome"]["result"]["stdout"] == text
    assert not list(directory.glob(".pending-*"))


@pytest.mark.parametrize("existing", ["directory", "file", "symlink", "dangling"])
def test_refuses_existing_destination_without_changing_it(tmp_path, existing):
    directory = tmp_path / "results"
    target = tmp_path / "target"
    target.mkdir()
    if existing == "directory":
        directory.mkdir()
    elif existing == "file":
        directory.write_text("keep")
    else:
        directory.symlink_to(target if existing == "symlink" else tmp_path / "missing")
    with pytest.raises(BatchResultsError, match="fresh --results-dir"):
        BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input")
    assert list(target.iterdir()) == []
    if existing == "file":
        assert directory.read_text() == "keep"


@pytest.mark.parametrize("unsafe", ["traversal", "symlink-parent", "missing-parent"])
def test_refuses_unsafe_or_missing_parent(tmp_path, unsafe):
    if unsafe == "traversal":
        directory = tmp_path / ".." / "escaped-results"
    elif unsafe == "symlink-parent":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        directory = alias / "results"
    else:
        directory = tmp_path / "missing" / "results"
    with pytest.raises(BatchResultsError):
        BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input")
    assert not (tmp_path / "results").exists()
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_never_overwrites_an_existing_result_file_or_symlink(tmp_path, symlink):
    directory = tmp_path / "results"
    target = tmp_path / "existing"
    target.write_text("do not change")
    with BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input") as writer:
        destination = directory / "command-000001.json"
        if symlink:
            destination.symlink_to(target)
        else:
            destination.write_text("original artifact")
        with pytest.raises(BatchResultsError, match="cannot publish"):
            writer.write_outcome(CommandOutcome("one", "succeeded", native_result()))
        assert destination.read_text() == ("do not change" if symlink else "original artifact")
        assert target.read_text() == "do not change"
        assert not list(directory.glob(".pending-*"))


def test_replaced_directory_cannot_redirect_writes(tmp_path):
    directory = tmp_path / "results"
    moved = tmp_path / "original"
    diverted = tmp_path / "diverted"
    diverted.mkdir()
    with BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input") as writer:
        directory.rename(moved)
        directory.symlink_to(diverted, target_is_directory=True)
        with pytest.raises(BatchResultsError, match="was replaced"):
            writer.write_outcome(CommandOutcome("one", "succeeded", native_result()))
    assert list(diverted.iterdir()) == []
    assert [path.name for path in moved.iterdir()] == ["batch.json"]


def test_serialization_failure_publishes_no_file(tmp_path):
    directory = tmp_path / "results"
    with BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input") as writer:
        with pytest.raises(BatchResultsError, match="not JSON serializable"):
            writer._publish("command-000001.json", {"bad": object()})
    assert [path.name for path in directory.iterdir()] == ["batch.json"]


def test_duplicate_unknown_and_nonterminal_notifications_are_rejected(tmp_path):
    directory = tmp_path / "results"
    outcome = CommandOutcome("one", "failed", error="submit failed")
    with BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input") as writer:
        for invalid in (CommandOutcome("unknown", "skipped"), CommandOutcome("one", "running")):
            with pytest.raises(BatchResultsError):
                writer.write_outcome(invalid)
        writer.write_outcome(outcome)
        with pytest.raises(BatchResultsError, match="already published"):
            writer.write_outcome(outcome)
        assert writer.complete(BatchResult((outcome,), 1))
    assert read_json(directory / "command-000001.json")["outcome"] == outcome.to_dict()
    assert read_json(directory / "complete.json")["state"] == "complete"
    assert read_json(directory / "complete.json")["result"]["ok"] is False


def test_missing_artifacts_cannot_produce_success_marker(tmp_path):
    directory = tmp_path / "results"
    with BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input") as writer:
        assert not writer.complete(BatchResult((CommandOutcome("one", "succeeded"),), 1))
    marker = read_json(directory / "complete.json")
    assert marker["state"] == "error"
    assert marker["missing_command_ids"] == ["one"]
    assert marker["artifact_count"] == 0
    assert marker["result"]["ok"] is True


def test_callback_link_failure_retains_missing_receipt_and_cleans_staging(tmp_path, monkeypatch):
    directory = tmp_path / "results"
    original_link = os.link

    def fail_command_link(source, destination, **kwargs):
        if destination.startswith("command-"):
            raise OSError("disk unavailable")
        return original_link(source, destination, **kwargs)

    with BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input") as writer:
        monkeypatch.setattr(os, "link", fail_command_link)
        with pytest.raises(BatchResultsError, match="disk unavailable"):
            writer.write_outcome(CommandOutcome("one", "succeeded", native_result()))
        assert not writer.complete(None, errors=[("one", "disk unavailable")])
    assert not (directory / "command-000001.json").exists()
    assert not list(directory.glob(".pending-*"))
    assert read_json(directory / "complete.json")["state"] == "error"


@pytest.mark.parametrize("point", ["file-sync", "directory-sync", "unlink"])
def test_filesystem_failure_is_visible_even_after_atomic_publication(tmp_path, monkeypatch, point):
    directory = tmp_path / "results"
    original_sync = os.fsync
    original_unlink = os.unlink

    def fail_sync(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (point == "file-sync" and not is_directory) or (point == "directory-sync" and is_directory):
            raise OSError("sync unavailable")
        return original_sync(fd)

    def fail_unlink(path, **kwargs):
        if point == "unlink":
            raise OSError("unlink unavailable")
        return original_unlink(path, **kwargs)

    outcome = CommandOutcome("one", "succeeded", native_result())
    result = BatchResult((outcome,), 1)
    with BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input") as writer:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_sync)
            patch.setattr(os, "unlink", fail_unlink)
            with pytest.raises(BatchResultsError, match="unavailable"):
                writer.write_outcome(outcome)
        assert not writer.complete(result)
    marker = read_json(directory / "complete.json")
    assert marker["state"] == "error"
    assert marker["missing_command_ids"] == ["one"]
    if point == "file-sync":
        assert not (directory / "command-000001.json").exists()
    else:
        assert read_json(directory / "command-000001.json")["outcome"] == outcome.to_dict()


@pytest.mark.parametrize("state", ["failed", "skipped", "cancelled"])
def test_artifacts_preserve_all_native_non_success_states(tmp_path, state):
    directory = tmp_path / "results"
    outcome = CommandOutcome("one", state, error="native failure", skipped_due_to=("dependency",))
    with BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input") as writer:
        writer.write_outcome(outcome)
        assert writer.complete(BatchResult((outcome,), 1))
    assert read_json(directory / "command-000001.json")["outcome"] == outcome.to_dict()


def test_staging_cleanup_error_cannot_replace_interruption(tmp_path, monkeypatch, caplog):
    directory = tmp_path / "results"

    def interrupted_sync(fd):
        raise KeyboardInterrupt

    def failed_unlink(*args, **kwargs):
        raise OSError("staging cleanup unavailable")

    with BatchResultsWriter(directory, manifest("one"), source="-", raw_manifest="input") as writer:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", interrupted_sync)
            patch.setattr(os, "unlink", failed_unlink)
            with pytest.raises(KeyboardInterrupt):
                writer.write_outcome(CommandOutcome("one", "succeeded", native_result()))
        assert not writer.complete(None, errors=[(None, "KeyboardInterrupt")], interrupted=True)
    assert "staging cleanup unavailable" in caplog.text
    assert not (directory / "command-000001.json").exists()
    assert read_json(directory / "complete.json")["state"] == "interrupted"
