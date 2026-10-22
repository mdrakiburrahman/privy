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
    for option in ("--relays-file", "--allow-origin", "--probe-ttl-s", "PRIVY_HUB_HOME"):
        assert option in help_text
    assert "privy hub serve --port 7748" in help_text


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

        assert main(["hub", "status", "--home", str(home), "--json", "--refresh"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["targets"] == 4
    finally:
        runtime.shutdown()
        thread.join(5)


def test_hub_status_explains_a_missing_hub(tmp_path, capsys):
    assert main(["hub", "status", "--home", str(tmp_path)]) == 2
    assert "start 'privy hub serve' first" in capsys.readouterr().err

    (tmp_path / "hub-token").write_text("token", encoding="utf-8")
    assert main(["hub", "status", "--home", str(tmp_path), "--url", "http://127.0.0.1:9"]) == 1
    assert "hub is not reachable" in capsys.readouterr().err


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

    monkeypatch.setattr("privy.hub.app.build_hub", fake_build_hub)
    caplog.set_level(logging.INFO, logger="privy")

    assert main(["hub", "serve", "--home", str(tmp_path), "--port", "0", "--allow-origin", "http://x"]) == 0

    assert captured["home"] == tmp_path and captured["port"] == 0
    assert captured["allowed_origins"] == ["http://x"]
    assert captured["closed"] is True
    assert "Waiting for" in caplog.text
