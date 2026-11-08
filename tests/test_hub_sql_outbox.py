import pytest

from privy.hub.sql_store import DurableOutbox, StorageError


def test_outbox_replays_ordered_records_after_restart(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    outbox = DurableOutbox(path, max_bytes=4096)
    first = outbox.append("job", "full-id", {"state": "running"}, "owner")
    second = outbox.append("job", "full-id", {"state": "succeeded"}, "owner")
    outbox.close()
    restored = DurableOutbox(path, max_bytes=4096)
    rows = restored.pending()
    assert [row[0] for row in rows] == [first, second]
    restored.ack(first)
    assert restored.pending()[0][0] == second
    restored.close()


def test_outbox_full_rejects_without_dropping_pending_data(tmp_path):
    outbox = DurableOutbox(tmp_path / "outbox.sqlite3", max_bytes=512)
    outbox.append("output", "one", {"data": "x" * 200}, "owner")
    with pytest.raises(StorageError, match="full"):
        outbox.append("output", "two", {"data": "x" * 400}, "owner")
    assert len(outbox.pending()) == 1
    assert outbox.status()["rejected_writes"] == 1
    outbox.close()
    restored = DurableOutbox(tmp_path / "outbox.sqlite3", max_bytes=512)
    assert restored.status()["rejected_writes"] == 1
    restored.close()


def test_acknowledgment_does_not_reset_sequence(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    outbox = DurableOutbox(path, max_bytes=4096)
    first = outbox.append("audit", "one", {}, "owner")
    outbox.ack(first)
    outbox.close()
    outbox = DurableOutbox(path, max_bytes=4096)
    assert outbox.append("audit", "two", {}, "owner") > first
    outbox.close()
