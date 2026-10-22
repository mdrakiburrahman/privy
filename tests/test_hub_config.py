import json
import os

import pytest
from hub_fakes import relays_document, write_relays

from privy.hub.config import RelaysFileError, RelaysStore, parse_relays_document, read_relays_file


def test_targets_inherit_relay_credentials_and_get_stable_ids():
    relays = parse_relays_document(relays_document())

    assert [relay.name for relay in relays] == ["dev", "contoso-lab"]
    dev, lab = relays
    assert [target.id for target in dev.targets] == ["dev/box1_windows", "dev/box2_windows"]
    assert dev.targets[1].description == "build box"
    assert dev.metadata == {"subscription_id": "00000000-0000-0000-0000-000000000000"}
    assert [target.id for target in lab.targets] == ["contoso-lab/box1_windows", "contoso-lab/gpu1_linux"]
    gpu = lab.targets[1]
    assert (gpu.keyrule, gpu.key) == ("privy-send", "bmFtZXNwYWNl")
    assert gpu.relay_client_kwargs(ttl_seconds=60) == {
        "namespace": "contoso-lab.servicebus.windows.net",
        "path": "gpu1_linux",
        "keyrule": "privy-send",
        "key": "bmFtZXNwYWNl",
        "ttl_seconds": 60,
    }


def test_keys_never_appear_in_target_repr():
    target = parse_relays_document(relays_document())[0].targets[0]

    assert "a2V5MQ==" not in repr(target)


def test_injected_tokens_are_supported():
    document = {
        "relays": [
            {
                "namespace": "ns",
                "hybrid_connections": [
                    {"path": "box", "token": "SharedAccessSignature sr=x&sig=y&se=1&skn=z"}
                ],
            }
        ]
    }

    target = parse_relays_document(document)[0].targets[0]

    assert target.relay_client_kwargs(ttl_seconds=60) == {
        "namespace": "ns",
        "path": "box",
        "token": "SharedAccessSignature sr=x&sig=y&se=1&skn=z",
    }


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "JSON object"),
        ({"version": 2, "relays": []}, "unsupported relays file version"),
        ({"relays": {}}, "relays must be a list"),
        ({"relays": [{"hybrid_connections": []}]}, "namespace"),
        ({"relays": [{"namespace": "ns"}]}, "hybrid_connections must be a list"),
        ({"relays": [{"namespace": "ns", "hybrid_connections": ["bad path"]}]}, "path must be"),
        ({"relays": [{"namespace": "ns", "hybrid_connections": ["box"]}]}, "needs keyrule and key"),
        (
            {"relays": [{"namespace": "ns", "keyrule": "r", "hybrid_connections": ["box"]}]},
            "key must be a non-empty string",
        ),
        (
            {
                "relays": [
                    {
                        "namespace": "ns",
                        "token": "SharedAccessSignature sr=x",
                        "keyrule": "r",
                        "key": "k",
                        "hybrid_connections": [],
                    }
                ]
            },
            "mutually exclusive",
        ),
        (
            {"relays": [{"namespace": "ns", "keyrule": "r", "key": "k", "hybrid_connections": ["a", "A"]}]},
            "duplicate hybrid connection",
        ),
        (
            {
                "relays": [
                    {"namespace": "ns", "keyrule": "r", "key": "k", "hybrid_connections": []},
                    {
                        "namespace": "ns.servicebus.windows.net",
                        "keyrule": "r",
                        "key": "k",
                        "hybrid_connections": [],
                    },
                ]
            },
            "duplicate relay name",
        ),
    ],
)
def test_invalid_documents_are_rejected_with_their_location(document, message):
    with pytest.raises(RelaysFileError, match=message):
        parse_relays_document(document)


def test_relays_file_tolerates_the_windows_powershell_bom(tmp_path):
    path = tmp_path / "relays.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(relays_document()).encode())

    assert len(read_relays_file(path)) == 2


def _touch(path, ns):
    os.utime(path, ns=(ns, ns))


def test_store_serves_nothing_until_hydrated_then_hot_reloads(tmp_path):
    path = tmp_path / "relays.json"
    store = RelaysStore(path, check_interval_s=0)

    empty = store.snapshot()
    assert empty.relays == () and empty.file_exists is False and empty.error is None

    write_relays(path)
    _touch(path, 1_000_000_000)
    loaded = store.snapshot()
    assert len(loaded.targets) == 4 and loaded.error is None

    path.write_text("{not json", encoding="utf-8")
    _touch(path, 2_000_000_000)
    broken = store.snapshot()
    assert len(broken.targets) == 4
    assert "not valid UTF-8 JSON" in broken.error

    document = relays_document()
    document["relays"] = document["relays"][:1]
    write_relays(path, document)
    _touch(path, 3_000_000_000)
    fixed = store.snapshot()
    assert [target.id for target in fixed.targets] == ["dev/box1_windows", "dev/box2_windows"]
    assert fixed.error is None

    path.unlink()
    assert store.snapshot().relays == ()


def test_store_checks_the_file_at_most_once_per_interval(tmp_path):
    path = write_relays(tmp_path / "relays.json")
    now = [0.0]
    store = RelaysStore(path, check_interval_s=5, clock=lambda: now[0])

    assert len(store.snapshot().targets) == 4
    path.unlink()
    now[0] = 4.0
    assert len(store.snapshot().targets) == 4
    now[0] = 5.0
    assert store.snapshot().targets == ()
