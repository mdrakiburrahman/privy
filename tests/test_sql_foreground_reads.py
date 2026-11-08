import base64
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_shared_jobs import Backend

from privy.hub.shared_jobs import SharedJob
from privy.hub.sql_store import SqlBackend


class BatchedBackend(Backend):
    def __init__(self):
        super().__init__()
        self.reads = []
        self.claims = []
        self.before_claim = lambda: None

    def read_view_output(self, job_id, ranges):
        self.reads.append(ranges)
        offsets, chunks = {}, {}
        for stream, (offset, total, limit) in ranges.items():
            offsets[stream] = self.read_cursors(job_id, (stream,))[stream] if offset is None else offset
            begin = min(max(0, offsets[stream]), total)
            chunks[stream] = self.output(job_id, stream, begin, max(0, min(limit, total - begin)))
        return offsets, chunks

    def advance_cursors(self, job_id, ends):
        self.claims.append(ends)
        self.before_claim()
        with self.lock:
            return {stream: self.advance_cursor(job_id, stream, end) for stream, end in ends.items()}


def make_job(backend, stdout=b"", stderr=b"", *, state="succeeded"):
    record = {
        "job_id": "job-1",
        "target": "dev/box_windows",
        "type": "exec",
        "spec": {"language": "bash", "mode": "subprocess"},
        "origin": "test",
        "owner": "one",
        "state": state,
        "created_at": 1,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
    }
    backend.put("job", "job-1", record)
    for index, (stream, data) in enumerate((("stdout", stdout), ("stderr", stderr))):
        if data:
            backend.put(
                "output",
                f"job-1:{index}",
                {
                    "job_id": "job-1",
                    "target": "job-1",
                    "index": index,
                    "stream": stream,
                    "start": 0,
                    "end": len(data),
                    "data": base64.b64encode(data).decode(),
                },
            )
    return SharedJob(backend, record)


def test_view_batches_both_stream_reads_and_positive_claims():
    backend = BatchedBackend()
    job = make_job(backend, b"hello", b"error")
    result = job.view(max_output_bytes=10, refresh=False)
    assert (result["stdout"], result["stderr"]) == ("hello", "error")
    assert len(backend.reads) == 1
    assert backend.claims == [{"stdout": 5, "stderr": 5}]
    result = job.view(max_output_bytes=10, refresh=False)
    assert (result["stdout"], result["stderr"]) == ("", "")
    assert len(backend.claims) == 1


def test_view_batch_preserves_explicit_offsets_and_concurrent_consumption():
    backend = BatchedBackend()
    job = make_job(backend, b"hello", b"error")
    backend.before_claim = lambda: backend.advance_cursor("job-1", "stderr", 3)
    result = job.view(max_output_bytes=10, offsets={"stdout": 1}, refresh=False)
    assert (result["stdout"], result["stderr"]) == ("ello", "or")
    assert result["stderr_offset"] == 5
    assert backend.claims == [{"stderr": 5}]
    assert backend.read_cursors("job-1", ("stdout",)) == {"stdout": 0}


def test_batch_missing_chunk_does_not_claim_or_skip_output():
    backend = BatchedBackend()
    job = make_job(backend, b"hello")
    del backend.records["output", "job-1:0"]
    result = job.view(max_output_bytes=10, refresh=False)
    assert result["stdout"] == "" and result["stdout_offset"] == 0
    assert backend.claims == []
    backend.put(
        "output",
        "job-1:0",
        {"stream": "stdout", "target": "job-1", "start": 2, "end": 5, "data": "bGxv"},
    )
    assert job.view(max_output_bytes=10, refresh=False)["stdout_offset"] == 0


def test_batch_utf8_pages_and_nonadvancing_reads():
    backend = BatchedBackend()
    job = make_job(backend, ("\u20ac" * 2).encode())
    assert job.view(max_output_bytes=4, refresh=False)["stdout"] == "\u20ac"
    assert job.view(max_output_bytes=4, refresh=False)["stdout"] == "\u20ac"
    assert backend.claims == [{"stdout": 3}, {"stdout": 6}]
    assert job.view(max_output_bytes=6, offsets={"stdout": 0}, advance=False, refresh=False)["stdout"] == (
        "\u20ac" * 2
    )
    assert len(backend.claims) == 2


class Cursor:
    def __init__(self, results):
        self.results = results
        self.index = 0
        self.queries = []

    def execute(self, query, *params):
        self.queries.append((query, params))

    def fetchall(self):
        return self.results[self.index]

    def nextset(self):
        self.index += 1
        return self.index < len(self.results)


def sql_backend(results):
    cursor = Cursor(results)
    acquisitions = []

    @contextmanager
    def transaction():
        acquisitions.append(1)
        yield cursor

    return SimpleNamespace(transaction=transaction), cursor, acquisitions


def test_sql_view_output_is_one_execute_and_one_acquisition():
    backend, cursor, acquisitions = sql_backend(
        [[("stdout", 7), ("stderr", 0)], [("stdout", json.dumps({"start": 7, "end": 9, "data": "aGk="}))]]
    )
    offsets, chunks = SqlBackend.read_view_output(
        backend, "job-1", {"stdout": (None, 9, 10), "stderr": (0, 0, 10)}
    )
    assert offsets == {"stdout": 7, "stderr": 0}
    assert chunks == {"stdout": [{"start": 7, "end": 9, "data": "aGk="}], "stderr": []}
    assert len(acquisitions) == len(cursor.queries) == 1
    query, params = cursor.queries[0]
    assert "INSERT @ranges" in query and "r.offset IS NULL" in query
    assert params == ("stdout", None, 9, 10, "stderr", 0, 0, 10, "job-1", "job-1")
    assert "UPDLOCK" not in query


def test_sql_positive_claims_lock_in_order_and_batch_transactionally():
    backend, cursor, acquisitions = sql_backend([[("stderr", 3), ("stdout", 7)]])
    assert SqlBackend.advance_cursors(backend, "job-1", {"stdout": 9, "stderr": 5}) == {
        "stderr": 3,
        "stdout": 7,
    }
    assert len(acquisitions) == len(cursor.queries) == 1
    query, params = cursor.queries[0]
    assert query.count("WITH(UPDLOCK,HOLDLOCK)") == 2
    assert query.count("ELSE offset END") == 2
    assert params[1] == "stderr" and params[11] == "stdout"
    assert SqlBackend.advance_cursors(backend, "job-1", {}) == {}
    assert len(acquisitions) == 1
    with pytest.raises(ValueError):
        SqlBackend.advance_cursors(backend, "job-1", {"stdout": 0})
