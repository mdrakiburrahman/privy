from __future__ import annotations

import base64
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from privy import terminal_backends
from privy.terminal import TerminalManager
from privy.terminal_backends import supported_shells

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" and os.name != "nt",
    reason="native terminals support Linux and Windows",
)
OWNER = "native-test-owner-0123456"


def call(manager, action, **args):
    return manager.handle(
        {"kind": "terminal", "protocol_version": 1, "owner": OWNER, "action": action, **args}
    )


def send(manager, session, code, *, user_input=True):
    result = call(
        manager,
        "write",
        session_id=session,
        seq=manager._sessions[session].next_seq,
        data_b64=base64.b64encode(code.encode()).decode(),
        user_input=user_input,
    )
    assert result["ok"], result


def until(manager, session, expected, offset=0):
    output, carry = b"", b""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = call(manager, "read", session_id=session, offset=offset, wait_s=1)
        assert result["ok"], result
        chunk = base64.b64decode(result["data_b64"])
        output += chunk
        found = output.find(expected)
        end = found + len(expected) if found >= 0 else len(output)
        consumed = chunk[: end - (len(output) - len(chunk))]
        queries = (carry + consumed).count(b"\x1b[6n")
        if queries:
            send(manager, session, "\x1b[1;1R" * queries, user_input=False)
        carry = (carry + consumed)[-3:]
        offset = result["offset"]
        if found >= 0:
            return output[:end], offset - (len(output) - end)
    pytest.fail(f"Expected {expected!r} in native terminal output: {output!r}")


@pytest.mark.parametrize("shell", ["powershell", "bash"])
def test_native_shell_preserves_state_supports_interactive_input_interrupt_resize_and_cleanup(shell):
    if shell not in supported_shells():
        pytest.skip(f"native {shell} is unavailable")
    manager = TerminalManager()
    try:
        opened = call(
            manager, "open", request_id="native-open-request-012345", shell=shell, cols=120, rows=40
        )
        assert opened["ok"] and opened["state"] == "running", opened
        session = opened["session_id"]
        backend = manager._sessions[session].backend
        if shell == "bash":
            send(manager, session, "PS1='PR''IVY> '; PRIVY_STATE=hello; cd /tmp\r")
            send(manager, session, 'printf \'STATE=%s:%s\\n\' "$PRIVY_STATE" "$PWD"\r')
            _, offset = until(manager, session, b"STATE=hello:/tmp")
            assert call(manager, "resize", session_id=session, cols=110, rows=42)["ok"]
            _, offset = until(manager, session, b"PRIVY> ", offset)
            send(manager, session, "stty size\r")
            _, offset = until(manager, session, b"42 110", offset)
            send(manager, session, "read -p 'ANS''WER> ' value; printf 'REPLY=%s\\n' \"$value\"\r")
            _, offset = until(manager, session, b"ANSWER>", offset)
            send(manager, session, "native-interactive\r")
            _, offset = until(manager, session, b"REPLY=native-interactive", offset)
            send(manager, session, "printf 'SLEEP%s\\n' ING; sleep 60\r")
            _, offset = until(manager, session, b"SLEEPING\r\n", offset)
            send(manager, session, "\x03")
            _, offset = until(manager, session, b"PRIVY> ", offset)
            send(manager, session, "printf 'STILL=%s\\n' \"$PRIVY_STATE\"\r")
            until(manager, session, b"STILL=hello", offset)
        else:
            send(manager, session, "function prompt { 'PR'+'IVY> ' }; $PrivyState='hello'\r")
            send(manager, session, "Write-Output ('STATE=' + $PrivyState)\r")
            _, offset = until(manager, session, b"STATE=hello")
            assert call(manager, "resize", session_id=session, cols=110, rows=42)["ok"]
            send(manager, session, "$answer=Read-Host 'ANSWER'; Write-Output ('REPLY=' + $answer)\r")
            _, offset = until(manager, session, b"ANSWER: ", offset)
            send(manager, session, "native-interactive\r")
            _, offset = until(manager, session, b"REPLY=native-interactive", offset)
            send(manager, session, "Write-Output ('SLEEP'+'ING'); Start-Sleep 60\r")
            _, offset = until(manager, session, b"SLEEPING\r\n", offset)
            send(manager, session, "\x03")
            _, offset = until(manager, session, b"PRIVY> ", offset)
            send(manager, session, "Write-Output ('STILL=' + $PrivyState)\r")
            until(manager, session, b"STILL=hello", offset)
        assert call(manager, "close", session_id=session)["ok"]
        assert not backend.alive()
        assert not manager._sessions[session].reader.is_alive()
    finally:
        manager.close()


def test_windows_only_advertises_resize_safe_native_shells(monkeypatch):
    monkeypatch.setitem(sys.modules, "winpty", SimpleNamespace())
    monkeypatch.setattr(terminal_backends, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(sys, "getwindowsversion", lambda: SimpleNamespace(build=22631), raising=False)
    monkeypatch.setattr(
        terminal_backends,
        "available_runtimes",
        lambda: {
            "powershell": r"C:\Windows\powershell.exe",
            "bash": r"C:\Git\bin\bash.exe",
        },
    )
    assert supported_shells() == {"powershell": r"C:\Windows\powershell.exe"}
    with pytest.raises(ValueError, match="native interactive bash is unavailable"):
        terminal_backends.spawn("bash", 80, 24)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process-tree ownership")
def test_windows_cleanup_terminates_child_processes():
    import ctypes
    from ctypes import wintypes as w

    manager = TerminalManager()
    handle = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    try:
        opened = call(manager, "open", request_id="windows-process-tree-012345", shell="powershell")
        assert opened["ok"], opened
        session = opened["session_id"]
        send(
            manager,
            session,
            (
                "$p=[Diagnostics.ProcessStartInfo]::new();$p.FileName=(Get-Process -Id $PID).Path;"
                "$p.Arguments='-NoProfile -Command Start-Sleep 600';$p.UseShellExecute=$false;"
                "$p.CreateNoWindow=$true;$child=[Diagnostics.Process]::Start($p);"
                "Write-Output ('CHILD='+$child.Id+':END')\r"
            ),
        )
        output, _ = until(manager, session, b":END\r\n")
        child = int(re.search(rb"CHILD=(\d+):END", output)[1])
        handle = kernel.OpenProcess(0x00100000, False, child)
        assert handle, ctypes.WinError(ctypes.get_last_error())
        assert kernel.WaitForSingleObject(handle, 0) == 0x102
        assert call(manager, "close", session_id=session)["ok"]
        assert kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        manager.close()
        if handle:
            kernel.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PTY process-session ownership")
def test_cleanup_terminates_foreground_and_background_process_groups():
    manager = TerminalManager()
    try:
        opened = call(manager, "open", request_id="process-tree-open-01234567", shell="bash")
        assert opened["ok"], opened
        session = opened["session_id"]
        backend = manager._sessions[session].backend
        send(manager, session, "sleep 600 & printf 'FORE%s\\n' GROUND; sleep 600\r")
        until(manager, session, b"FOREGROUND\r\n")
        deadline = time.monotonic() + 2
        children = []
        while len(children) < 3 and time.monotonic() < deadline:
            children = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    fields = (entry / "stat").read_text().rpartition(")")[2].split()
                except (FileNotFoundError, PermissionError):
                    continue
                if int(fields[3]) == backend.pid:
                    children.append(entry / "stat")
            if len(children) < 3:
                time.sleep(0.01)
        assert len(children) >= 3
        assert call(manager, "close", session_id=session)["ok"]
        for path in children:
            try:
                state = path.read_text().rpartition(")")[2].split()[0]
            except FileNotFoundError:
                continue
            assert state == "Z"
    finally:
        manager.close()
