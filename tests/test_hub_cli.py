import json
import logging
import threading

from hub_fakes import FakeListener, FakeRelayClient, write_relays

from privy.cli import build_parser, main
from privy.hub.app import build_hub


def test_top_level_help_documents_the_hub():
    help_text = build_parser().format_help()

    for command in ("hub", "hub serve", "hub status"):
        assert f"FULL COMMAND HELP: privy {command}" in help_text
    for option in ("--relays-file", "--echo-output", "--probe-ttl-s", "PRIVY_HUB_HOME"):
        assert option in help_text
    assert "--allow-origin" not in help_text and "hub-token" not in help_text
    assert "privy hub serve --port 7748" in help_text
    assert "Point an LLM at http://127.0.0.1:7748/" in help_text


def test_hub_status_prints_every_target(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    write_relays(home / "relays.json")
    listeners = {"dev/box2_windows": FakeListener(legacy=True)}
    runtime = build_hub(
        home=home,
        port=0,
        version="test",
        client_factory=lambda target: FakeRelayClient(
            listeners.setdefault(target.id, FakeListener(hostname=target.path.upper()))
        ),
    )
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        assert main(["hub", "status", "--home", str(home)]) == 0
        table = capsys.readouterr().out
        assert f"privy hub at {runtime.url}" in table
        assert "targets: 4, online 4" in table
        assert "dev/box1_windows" in table and "BOX1_WINDOWS" in table
        assert "older" in table
        assert f"Point an LLM at {runtime.url}/" in table

        assert main(["hub", "status", "--home", str(home), "--json", "--refresh"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["targets"] == 4
    finally:
        runtime.shutdown()
        thread.join(5)


def test_hub_status_explains_an_unreachable_hub(tmp_path, capsys):
    # Never fall back to the default port here: a real hub may be listening on it.
    assert main(["hub", "status", "--home", str(tmp_path), "--url", "http://127.0.0.1:9"]) == 1
    assert "hub is not reachable at http://127.0.0.1:9" in capsys.readouterr().err

    (tmp_path / "hub-state.json").write_text(
        json.dumps({"url": "http://127.0.0.1:9", "pid": 1}), encoding="utf-8"
    )
    assert main(["hub", "status", "--home", str(tmp_path)]) == 1
    assert "hub is not reachable at http://127.0.0.1:9" in capsys.readouterr().err


def test_hub_serve_builds_the_runtime_and_stops_on_interrupt(tmp_path, monkeypatch, caplog):
    captured = {}

    class FakeRuntime:
        url = "http://127.0.0.1:7748"
        relays_file = tmp_path / "relays.json"

        def __init__(self):
            self.service = type(
                "Service",
                (),
                {
                    "summary": lambda self: {
                        "relays_file": {"exists": False},
                        "relays": 0,
                        "targets": 0,
                    }
                },
            )()
            self.server = type(
                "Server", (), {"server_close": lambda self: captured.setdefault("closed", True)}
            )()

        def serve_forever(self):
            raise KeyboardInterrupt

    def fake_build_hub(**kwargs):
        captured.update(kwargs)
        return FakeRuntime()

    echo = []
    monkeypatch.setattr("privy.hub.app.build_hub", fake_build_hub)
    monkeypatch.setattr("privy.executor.set_echo_output", echo.append)
    monkeypatch.delenv("PRIVY_ECHO_OUTPUT", raising=False)
    caplog.set_level(logging.INFO, logger="privy")
    (tmp_path / "hub-token").write_text("token from hub 0.2", encoding="utf-8")

    assert main(["hub", "serve", "--home", str(tmp_path), "--port", "0"]) == 0

    assert captured["home"] == tmp_path and captured["port"] == 0
    assert "allowed_origins" not in captured
    assert captured["closed"] is True
    assert echo == [True]
    assert "Waiting for" in caplog.text
    assert "hub-token is no longer used" in caplog.text
    assert "Point an LLM at http://127.0.0.1:7748/" in caplog.text
    assert "no authentication and listens on" not in caplog.text

    caplog.clear()
    assert main(["hub", "serve", "--home", str(tmp_path), "--host", "0.0.0.0", "--no-echo-output"]) == 0
    assert echo == [True, False]
    assert "no authentication and listens on 0.0.0.0" in caplog.text
