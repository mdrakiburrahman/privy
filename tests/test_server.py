import json
import threading
import time
from collections import defaultdict, deque

import websocket

import privy.server as server_mod
from privy.protocol import ExecRequest, ExecResponse
from privy.server import RelayServer


def _inline_frames(request_id: str, request: ExecRequest) -> list[str]:
    return [json.dumps({"request": {"id": request_id, "method": "POST", "body": True}}), request.to_json()]


class _FakeControlSocket:
    DISCONNECT = object()

    def __init__(self, frames, *, enforce_listener_thread_sends: bool = False):
        self._frames = deque(frames)
        self._lock = threading.Condition()
        self._closed = False
        self._listener_thread_id: int | None = None
        self._enforce_listener_thread_sends = enforce_listener_thread_sends
        self.sent_frames: list[tuple[int, str]] = []
        self.connected = threading.Event()

    @property
    def listener_thread_id(self) -> int | None:
        return self._listener_thread_id

    def settimeout(self, timeout: float) -> None:
        return None

    def recv(self):
        with self._lock:
            if self._listener_thread_id is None:
                self._listener_thread_id = threading.get_ident()
                self.connected.set()
            if self._closed:
                raise websocket.WebSocketConnectionClosedException("Connection to remote host was lost.")
            if self._frames:
                frame = self._frames.popleft()
                if frame is self.DISCONNECT:
                    self._closed = True
                    raise websocket.WebSocketConnectionClosedException("Connection to remote host was lost.")
                return frame
            raise websocket.WebSocketTimeoutException("timed out")

    def send(self, data: str) -> None:
        with self._lock:
            if self._closed:
                raise websocket.WebSocketConnectionClosedException("Connection to remote host was lost.")
            if self._enforce_listener_thread_sends and threading.get_ident() != self._listener_thread_id:
                self._closed = True
                raise websocket.WebSocketConnectionClosedException("control socket used off listener thread")
            self.sent_frames.append((threading.get_ident(), data))
            self._lock.notify_all()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._lock.notify_all()

    def wait_for_sent_frames(self, count: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._lock:
            while len(self.sent_frames) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._lock.wait(timeout=remaining)
            return True


def _decode_responses(control: _FakeControlSocket) -> dict[str, ExecResponse]:
    payloads = [frame for _, frame in control.sent_frames]
    assert len(payloads) % 2 == 0
    decoded: dict[str, ExecResponse] = {}
    for header, body in zip(payloads[0::2], payloads[1::2]):
        req_id = json.loads(header)["response"]["requestId"]
        decoded[req_id] = ExecResponse.from_json(body)
    return decoded


def test_inline_control_socket_responses_stay_on_listener_thread(monkeypatch):
    control = _FakeControlSocket(
        _inline_frames("req-1", ExecRequest(kind="python", code="print('ok')")),
        enforce_listener_thread_sends=True,
    )

    monkeypatch.setattr(server_mod.websocket, "create_connection", lambda _: control)
    monkeypatch.setattr(
        server_mod,
        "execute",
        lambda req: ExecResponse.from_output(exit_code=0, stdout=b"ok\n", stderr=b"", duration_ms=1),
    )

    server = RelayServer(namespace="ns", path="path", keyrule="rule", key="key", recv_timeout_s=0.01)
    server._pool = server_mod.ThreadPoolExecutor(max_workers=4, thread_name_prefix="privy-test")
    try:
        runner = threading.Thread(target=server._serve_once, daemon=True)
        runner.start()
        assert control.connected.wait(timeout=2)
        assert control.wait_for_sent_frames(2, timeout=2)
        server.stop()
        runner.join(timeout=2)
        assert not runner.is_alive()
    finally:
        if server._pool is not None:
            server._pool.shutdown(wait=True, cancel_futures=True)

    responses = _decode_responses(control)
    assert set(responses) == {"req-1"}
    assert responses["req-1"].stdout == b"ok\n"
    assert {thread_id for thread_id, _ in control.sent_frames} == {control.listener_thread_id}


class _FakeAsyncExecutor:
    def __init__(self, expected_jobs: int):
        self._expected_jobs = expected_jobs
        self._lock = threading.Lock()
        self._request_to_job: dict[str, str] = {}
        self.submit_counts: dict[str, int] = defaultdict(int)
        self.retry_submit_count = 0
        self._poll_request_ids: set[str] = set()
        self.initial_submits_seen = threading.Event()
        self.retry_submits_seen = threading.Event()
        self.all_polls_started = threading.Event()
        self.allow_initial_submit_responses = threading.Event()
        self.finish_polls = threading.Event()

    def __call__(self, req: ExecRequest) -> ExecResponse:
        if req.action == "submit":
            assert req.request_id
            with self._lock:
                job_id = self._request_to_job.get(req.request_id)
                first_submit = job_id is None
                if first_submit:
                    job_id = f"job-{len(self._request_to_job):02d}"
                    self._request_to_job[req.request_id] = job_id
                    self.submit_counts[req.request_id] += 1
                    if len(self._request_to_job) == self._expected_jobs:
                        self.initial_submits_seen.set()
                else:
                    self.retry_submit_count += 1
                    if self.retry_submit_count == self._expected_jobs:
                        self.retry_submits_seen.set()
            if first_submit:
                assert self.allow_initial_submit_responses.wait(timeout=5)
            return ExecResponse.from_output(
                exit_code=0,
                stdout=b"",
                stderr=b"",
                duration_ms=0,
                job_id=job_id,
                state="running",
            )

        if req.action == "poll":
            assert req.job_id
            with self._lock:
                self._poll_request_ids.add(req.job_id)
                if len(self._poll_request_ids) == self._expected_jobs:
                    self.all_polls_started.set()
                request_id = next(rid for rid, job_id in self._request_to_job.items() if job_id == req.job_id)
            self.finish_polls.wait(timeout=req.wait_s)
            if not self.finish_polls.is_set():
                return ExecResponse.from_output(
                    exit_code=0,
                    stdout=b"",
                    stderr=b"",
                    duration_ms=0,
                    job_id=req.job_id,
                    state="running",
                )
            return ExecResponse.from_output(
                exit_code=0,
                stdout=f"{request_id}\n".encode(),
                stderr=b"",
                duration_ms=0,
                job_id=req.job_id,
                state="done",
            )

        raise AssertionError(f"unexpected action: {req.action}")


def test_retry_submit_and_poll_survive_listener_reconnect_without_cross_contamination(monkeypatch):
    jobs = 32
    retry_submit_ids = [f"conn2-submit-{i:02d}" for i in range(jobs)]
    poll_ids = [f"conn2-poll-{i:02d}" for i in range(jobs)]

    first_frames = []
    second_frames = []
    for i in range(jobs):
        request_id = f"rid-{i:02d}"
        first_frames.extend(
            _inline_frames(
                f"conn1-submit-{i:02d}",
                ExecRequest(
                    kind="python",
                    code=f"print('{request_id}')",
                    mode="inprocess",
                    action="submit",
                    request_id=request_id,
                ),
            )
        )
        second_frames.extend(
            _inline_frames(
                retry_submit_ids[i],
                ExecRequest(
                    kind="python",
                    code=f"print('{request_id}')",
                    mode="inprocess",
                    action="submit",
                    request_id=request_id,
                ),
            )
        )
        second_frames.extend(
            _inline_frames(
                poll_ids[i],
                ExecRequest(
                    kind="python",
                    code=f"print('{request_id}')",
                    mode="inprocess",
                    action="poll",
                    job_id=f"job-{i:02d}",
                    request_id=request_id,
                    wait_s=2.0,
                ),
            )
        )
    first_frames.append(_FakeControlSocket.DISCONNECT)

    first = _FakeControlSocket(first_frames)
    second = _FakeControlSocket(second_frames, enforce_listener_thread_sends=True)
    connections = deque([first, second])
    executor = _FakeAsyncExecutor(expected_jobs=jobs)

    def fake_create_connection(_):
        assert connections, "unexpected extra reconnect"
        return connections.popleft()

    monkeypatch.setattr(server_mod.websocket, "create_connection", fake_create_connection)
    monkeypatch.setattr(server_mod, "execute", executor)

    server = RelayServer(
        namespace="ns",
        path="path",
        keyrule="rule",
        key="key",
        max_workers=64,
        recv_timeout_s=0.01,
    )
    runner = threading.Thread(target=server.serve_forever, daemon=True)

    started = time.monotonic()
    runner.start()
    assert first.connected.wait(timeout=2)
    assert executor.initial_submits_seen.wait(timeout=5)
    assert second.connected.wait(timeout=5)
    assert executor.retry_submits_seen.wait(timeout=5)
    assert executor.all_polls_started.wait(timeout=5)

    executor.finish_polls.set()
    assert second.wait_for_sent_frames(128, timeout=5)
    executor.allow_initial_submit_responses.set()
    server.stop()
    runner.join(timeout=5)
    assert not runner.is_alive()
    assert time.monotonic() - started < 5.0

    assert sum(executor.submit_counts.values()) == jobs
    assert all(count == 1 for count in executor.submit_counts.values())
    assert not first.sent_frames

    responses = _decode_responses(second)
    assert set(responses) == set(retry_submit_ids + poll_ids)
    assert all(responses[request_id].state == "running" for request_id in retry_submit_ids)
    assert all(
        responses[request_id].job_id == f"job-{i:02d}" for i, request_id in enumerate(retry_submit_ids)
    )
    assert all(responses[request_id].state == "done" for request_id in poll_ids)
    assert all(
        responses[request_id].stdout == f"rid-{i:02d}\n".encode() for i, request_id in enumerate(poll_ids)
    )
    assert {request_id for request_id in responses if request_id.startswith("conn1-")} == set()
    assert {thread_id for thread_id, _ in second.sent_frames} == {second.listener_thread_id}
