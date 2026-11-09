import base64
import contextlib
import gzip
import json
from types import SimpleNamespace

import pytest

from privy.hub.sql_store import SqlBackend, StorageError


def _encoded_page(rows):
    envelope = []
    for version, kind, key, payload in rows:
        envelope.append(
            {
                "change_version": base64.b64encode(version).decode("ascii"),
                "kind": kind,
                "id": key,
                "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }
        )
    return gzip.compress(json.dumps(envelope, ensure_ascii=False).encode("utf-16-le"))


class _Cursor:
    def __init__(self, upper, compressed):
        self.upper = upper
        self.compressed = compressed
        self.executed = []

    def execute(self, query, *params):
        self.executed.append((query, params))

    def fetchone(self):
        if len(self.executed) == 1:
            return (self.upper,)
        return (self.compressed,)


def _backend(cursor):
    backend = object.__new__(SqlBackend)

    @contextlib.contextmanager
    def transaction():
        yield cursor

    backend.transaction = transaction
    return backend


def test_read_changes_decodes_unicode_and_nested_payload_without_changing_values():
    lower = (100).to_bytes(8, "big")
    upper = (104).to_bytes(8, "big")
    payload = {"message": "雪だるま ☃", "nested": {"quoted": "a\\b\"c", "items": [1, {"ok": True}]}}
    cursor = _Cursor(upper, _encoded_page([(101 .to_bytes(8, "big"), "job", "工作☃", payload)]))

    watermark, rows, complete = SqlBackend.read_changes(_backend(cursor), lower, limit=2)

    assert rows == [("job", "工作☃", payload)]
    assert complete is True
    assert watermark == (103).to_bytes(8, "big")
    assert cursor.executed[0] == ("SELECT MIN_ACTIVE_ROWVERSION()", ())
    query, params = cursor.executed[1]
    assert "COMPRESS((SELECT TOP (?)" in query
    assert "ORDER BY change_version FOR JSON PATH))" in query
    assert "change_version>? AND change_version<?" in query
    assert "@@DBTS" not in query
    assert params == (2, lower, upper)


def test_read_changes_empty_page_advances_only_to_exclusive_safe_fence():
    upper = (12).to_bytes(8, "big")
    cursor = _Cursor(upper, _encoded_page([]))

    watermark, rows, complete = SqlBackend.read_changes(_backend(cursor), bytes(8), limit=5)

    assert rows == []
    assert complete is True
    assert watermark == (11).to_bytes(8, "big")
    assert "change_version<?" in cursor.executed[1][0]
    assert cursor.executed[1][1][-1] == upper


def test_read_changes_full_page_stops_at_last_ordered_rowversion():
    lower = bytes(8)
    upper = (12).to_bytes(8, "big")
    versions = [(8).to_bytes(8, "big"), (9).to_bytes(8, "big")]
    cursor = _Cursor(upper, _encoded_page([(v, "job", str(i), {"i": i}) for i, v in enumerate(versions)]))

    watermark, rows, complete = SqlBackend.read_changes(_backend(cursor), lower, limit=2)

    assert [row[1] for row in rows] == ["0", "1"]
    assert complete is False
    assert watermark == versions[-1]


@pytest.mark.parametrize(
    "compressed",
    [
        b"not gzip",
        gzip.compress(b"not UTF-16 JSON"),
        gzip.compress("{}".encode("utf-16-le")),
        gzip.compress("[{\"change_version\":\"AA==\",\"kind\":\"job\",\"id\":\"x\",\"payload\":\"{}\"}]".encode("utf-16-le")),
        gzip.compress("[{\"change_version\":\"!!!!!!!!!!!!\",\"kind\":\"job\",\"id\":\"x\",\"payload\":\"{}\"}]".encode("utf-16-le")),
        gzip.compress("[{\"change_version\":\"AAAAAAAAAAA=\",\"kind\":\"job\",\"id\":\"x\",\"payload\":\"{\"}]".encode("utf-16-le")),
    ],
)
def test_read_changes_rejects_malformed_or_truncated_transport(compressed):
    cursor = _Cursor((12).to_bytes(8, "big"), compressed[:-3] if compressed.startswith(b"\x1f\x8b") else compressed)

    with pytest.raises(StorageError):
        SqlBackend.read_changes(_backend(cursor), bytes(8))
