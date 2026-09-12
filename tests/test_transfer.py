import base64
import hashlib
import json

import pytest

from privy.transfer import (
    TransferError,
    TransferRequest,
    TransferResponse,
    _commit_partial,
    _download_partial_path,
    download_file,
    handle_transfer_request,
    upload_file,
)


def _post(raw: str) -> dict:
    return json.loads(handle_transfer_request(raw).to_json())


def test_upload_file_roundtrips_binary_chunks(tmp_path):
    source = tmp_path / "source.bin"
    destination = tmp_path / "remote.bin"
    payload = bytes(range(256)) * 20
    source.write_bytes(payload)

    result = upload_file(_post, source, destination, chunk_size=257)

    assert destination.read_bytes() == payload
    assert result.size == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.resumed_from == 0


def test_upload_resumes_matching_partial(tmp_path):
    source = tmp_path / "source.bin"
    destination = tmp_path / "remote.bin"
    payload = b"resume-me-" * 100
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    start = TransferResponse.from_json(
        _post(
            TransferRequest(
                action="upload_start",
                path=str(destination),
                size=len(payload),
                sha256=digest,
                chunk_size=128,
            ).to_json()
        )
    )
    first = payload[:128]
    TransferResponse.from_json(
        _post(
            TransferRequest(
                action="upload_chunk",
                path=str(destination),
                transfer_id=start.transfer_id,
                offset=0,
                size=len(payload),
                sha256=digest,
                data_b64=base64.b64encode(first).decode(),
                chunk_size=128,
            ).to_json()
        )
    )

    result = upload_file(_post, source, destination, chunk_size=128)

    assert result.resumed_from == len(first)
    assert destination.read_bytes() == payload


def test_upload_chunk_retry_is_idempotent(tmp_path):
    destination = tmp_path / "remote.bin"
    payload = b"same chunk"
    digest = hashlib.sha256(payload).hexdigest()
    start_request = TransferRequest(
        action="upload_start",
        path=str(destination),
        size=len(payload),
        sha256=digest,
    )
    start = TransferResponse.from_json(_post(start_request.to_json()))
    chunk = TransferRequest(
        action="upload_chunk",
        path=str(destination),
        transfer_id=start.transfer_id,
        offset=0,
        size=len(payload),
        sha256=digest,
        data_b64=base64.b64encode(payload).decode(),
    )

    first = TransferResponse.from_json(_post(chunk.to_json()))
    retried = TransferResponse.from_json(_post(chunk.to_json()))

    assert retried.offset == first.offset == len(payload)


def test_upload_refuses_existing_destination_without_overwrite(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("new")
    destination.write_text("old")

    with pytest.raises(TransferError) as exc:
        upload_file(_post, source, destination)

    assert exc.value.code == "destination_exists"
    assert destination.read_text() == "old"


def test_upload_overwrites_atomically_when_requested(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("new")
    destination.write_text("old")

    upload_file(_post, source, destination, overwrite=True)

    assert destination.read_text() == "new"


def test_upload_completion_replay_is_idempotent_without_overwrite(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"committed bytes")

    first = upload_file(_post, source, destination, chunk_size=4)
    replayed = upload_file(_post, source, destination, chunk_size=4)

    assert replayed.transfer_id == first.transfer_id
    assert replayed.resumed_from == len(b"committed bytes")
    assert destination.read_bytes() == b"committed bytes"


def test_upload_verifies_large_files_in_bounded_requests(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(bytes(range(100)))
    actions = []

    def recording_post(raw):
        actions.append(json.loads(raw)["action"])
        return _post(raw)

    upload_file(recording_post, source, destination, chunk_size=16)

    assert actions.count("upload_complete") > 1
    assert destination.read_bytes() == source.read_bytes()


def test_upload_rejects_checksum_mismatch_and_removes_partial(tmp_path):
    destination = tmp_path / "remote"
    payload = b"actual bytes"
    wrong_digest = hashlib.sha256(b"different bytes").hexdigest()
    start = TransferResponse.from_json(
        _post(
            TransferRequest(
                action="upload_start",
                path=str(destination),
                size=len(payload),
                sha256=wrong_digest,
            ).to_json()
        )
    )
    TransferResponse.from_json(
        _post(
            TransferRequest(
                action="upload_chunk",
                path=str(destination),
                transfer_id=start.transfer_id,
                offset=0,
                size=len(payload),
                sha256=wrong_digest,
                data_b64=base64.b64encode(payload).decode(),
            ).to_json()
        )
    )

    response = _post(
        TransferRequest(
            action="upload_complete",
            path=str(destination),
            transfer_id=start.transfer_id,
            offset=len(payload),
            size=len(payload),
            sha256=wrong_digest,
        ).to_json()
    )

    with pytest.raises(TransferError) as exc:
        TransferResponse.from_json(response)
    assert exc.value.code == "checksum_mismatch"
    assert not destination.exists()
    assert not list(tmp_path.glob("*.part"))


def test_zero_byte_upload_and_download(tmp_path):
    source = tmp_path / "empty"
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    source.touch()

    upload_file(_post, source, remote)
    result = download_file(_post, remote, local)

    assert remote.read_bytes() == local.read_bytes() == b""
    assert result.size == 0


def test_download_file_roundtrips_and_resumes(tmp_path):
    remote = tmp_path / "remote.bin"
    destination = tmp_path / "local.bin"
    payload = bytes(range(255, -1, -1)) * 20
    remote.write_bytes(payload)

    metadata = TransferResponse.from_json(
        _post(TransferRequest(action="download_start", path=str(remote), chunk_size=333).to_json())
    )
    partial = _download_partial_path(destination.resolve(), metadata.snapshot_id or "")
    partial.write_bytes(payload[:777])
    actions = []

    def recording_post(raw):
        actions.append(json.loads(raw)["action"])
        return _post(raw)

    result = download_file(recording_post, remote, destination, chunk_size=333)

    assert result.resumed_from == 777
    assert destination.read_bytes() == payload
    assert "download_hash" in actions


def test_download_start_does_not_hash_the_whole_file(tmp_path):
    remote = tmp_path / "remote"
    remote.write_bytes(b"x" * 1024)

    metadata = TransferResponse.from_json(
        _post(TransferRequest(action="download_start", path=str(remote)).to_json())
    )

    assert metadata.sha256 is None
    assert metadata.snapshot_id


def test_download_chunk_retry_is_idempotent(tmp_path):
    remote = tmp_path / "remote"
    remote.write_bytes(b"abcdef")
    metadata = TransferResponse.from_json(
        _post(TransferRequest(action="download_start", path=str(remote), chunk_size=3).to_json())
    )
    request = TransferRequest(
        action="download_chunk",
        path=str(remote),
        transfer_id=metadata.transfer_id,
        offset=0,
        chunk_size=3,
    )

    first = TransferResponse.from_json(_post(request.to_json()))
    replayed = TransferResponse.from_json(_post(request.to_json()))

    assert replayed == first


def test_atomic_no_overwrite_commit_never_clobbers_existing_file(tmp_path):
    partial = tmp_path / ".partial"
    destination = tmp_path / "destination"
    partial.write_text("new")
    destination.write_text("existing")

    with pytest.raises(TransferError) as exc:
        _commit_partial(partial, destination, overwrite=False, location="local")

    assert exc.value.code == "destination_exists"
    assert destination.read_text() == "existing"
    assert partial.read_text() == "new"


def test_download_refuses_existing_destination_without_overwrite(tmp_path):
    remote = tmp_path / "remote"
    destination = tmp_path / "local"
    remote.write_text("new")
    destination.write_text("old")

    with pytest.raises(TransferError) as exc:
        download_file(_post, remote, destination)

    assert exc.value.code == "destination_exists"
    assert destination.read_text() == "old"


def test_download_detects_source_change(tmp_path):
    remote = tmp_path / "remote"
    remote.write_text("before")
    metadata = TransferResponse.from_json(
        _post(TransferRequest(action="download_start", path=str(remote)).to_json())
    )
    remote.write_text("after-change")

    response = _post(
        TransferRequest(
            action="download_chunk",
            path=str(remote),
            transfer_id=metadata.transfer_id,
            offset=0,
        ).to_json()
    )

    with pytest.raises(TransferError) as exc:
        TransferResponse.from_json(response)
    assert exc.value.code == "source_changed"


def test_transfer_request_rejects_invalid_chunk_size():
    raw = json.dumps(
        {
            "kind": "file_transfer",
            "action": "download_start",
            "path": "/tmp/source",
            "chunk_size": 0,
        }
    )

    response = handle_transfer_request(raw)

    assert response.ok is False
    assert response.error == "bad_request"
