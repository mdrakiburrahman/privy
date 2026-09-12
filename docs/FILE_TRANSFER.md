# File transfer

Privy transfers individual files in bounded Relay requests. Upload and download are resumable and binary-safe.

## CLI

```bash
privy file upload ./local.bin /remote/path/local.bin
privy file download /remote/path/results.bin ./results.bin
```

Options:

```text
--chunk-size 1MiB
--overwrite
--json
```

The default raw chunk is 1 MiB and the server caps chunks at 2 MiB. Base64 and JSON overhead therefore remain below Azure Relay's per-message limit.

## Python

```python
upload = client.upload_file(
    "./local.bin",
    "/remote/path/local.bin",
    chunk_size=512 * 1024,
)

download = client.download_file(
    "/remote/path/results.bin",
    "./results.bin",
    overwrite=True,
)
```

An optional progress callback receives `(completed_bytes, total_bytes)`.

## Upload contract

1. The client calculates source size and SHA-256.
2. The listener creates or identifies a deterministic partial file beside the destination.
3. The listener reports its current byte offset so a retry can resume.
4. Each chunk must match the current offset. Replaying the most recent identical chunk is idempotent.
5. Completion verifies size and SHA-256 incrementally across bounded Relay requests.
6. The listener atomically replaces the destination only after verification and only when overwrite was explicit.

A checksum failure removes the corrupt partial. An interrupted valid partial remains available for the next matching upload. A small completion marker tied to the destination file identity makes the final operation idempotent if the Relay response is lost after commit.

## Download contract

1. The listener returns source size, a snapshot ID, and a unique download session without hashing the full file in one request.
2. The client resumes a deterministic local partial at its current size.
3. For a resumed prefix, bounded hash-only requests rebuild the server's SHA-256 state without retransmitting those bytes.
4. Every data chunk identifies its download session and advances the same server-side digest.
5. The final chunk returns the server SHA-256; the client verifies its complete local partial against it.
6. The client atomically moves the partial into place only after verification.

The listener checks source identity, size, and modification time before and after every bounded read and returns `source_changed` when they differ. Final SHA-256 proves that the completed client file matches the byte stream hashed and sent by the listener. Transfer does not create an immutable filesystem snapshot; writers capable of changing content while preserving all file metadata must coordinate externally or publish through an immutable/atomic source path.

## Paths and overwrite

Remote paths may be any file path available to the listener process. Local paths may be any file path available to the client process. This API is intentionally not a filesystem sandbox; privy already grants remote code execution to authenticated senders.

Only regular files are supported:

- Directories and special files are rejected.
- Destination parent directories must already exist.
- Existing destinations are rejected unless `overwrite=True` or `--overwrite`.
- Without overwrite, commit uses an atomic same-filesystem no-clobber link so a concurrently created destination is never replaced.
- With overwrite, replacement uses the operating system's atomic replacement operation.

## Result and errors

`TransferResult` contains:

```text
direction
source
destination
size
sha256
transfer_id
resumed_from
```

CLI progress is written to stderr. Normal metadata goes to stdout; `--json` emits the stable structured form. Failures identify a machine-readable code such as `destination_exists`, `offset_mismatch`, `source_changed`, or `checksum_mismatch`.
