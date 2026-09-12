import pytest

from privy import _relay
from privy._relay import (
    RelayCredential,
    RelayTokenAudienceError,
    RelayTokenError,
    RelayTokenExpiredError,
    create_sas_token,
    parse_sas_token,
    redact_relay_secrets,
    validate_sas_token,
)


def _token(*, audience: str = "ns.servicebus.windows.net/path", ttl_seconds: int = 60) -> str:
    return create_sas_token(
        audience.split("/", 1)[0],
        audience.split("/", 1)[1],
        "rule",
        "secret",
        ttl_seconds,
    )


def test_sas_token_mint_parse_validate_roundtrip(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    token = _token(ttl_seconds=90)

    claims = parse_sas_token(token)

    assert claims["sr"] == "http://ns.servicebus.windows.net/path"
    assert claims["se"] == 1_800_000_090
    assert claims["skn"] == "rule"
    assert validate_sas_token(token, "ns", "path", now=1_800_000_000) == claims


def test_sas_token_audience_may_cover_a_child_path(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    token = _token(audience="ns.servicebus.windows.net/root")

    validate_sas_token(token, "ns", "root/child", now=1_800_000_000)


def test_sas_token_rejects_expired_token(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    token = _token(ttl_seconds=10)

    with pytest.raises(RelayTokenExpiredError, match=r"relay token expired at .*Z"):
        validate_sas_token(token, "ns", "path", now=1_800_000_010)


def test_sas_token_rejects_wrong_audience(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    token = _token(audience="ns.servicebus.windows.net/other")

    with pytest.raises(RelayTokenAudienceError, match="does not cover"):
        validate_sas_token(token, "ns", "path", now=1_800_000_000)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "SharedAccessSignature sr=x&sig=y&se=1",
        "SharedAccessSignature sr=x&sig=y&se=nope&skn=z",
    ],
)
def test_parse_sas_token_rejects_malformed_values(token):
    with pytest.raises(RelayTokenError):
        parse_sas_token(token)


def test_relay_credential_requires_exactly_one_shape():
    with pytest.raises(ValueError, match="provide token"):
        RelayCredential(namespace="ns", path="path")
    with pytest.raises(ValueError, match="mutually exclusive"):
        RelayCredential(
            namespace="ns",
            path="path",
            keyrule="rule",
            key="key",
            token="token",
        )
    with pytest.raises(ValueError, match="provided together"):
        RelayCredential(namespace="ns", path="path", keyrule="rule")


def test_relay_credential_calls_provider_every_time(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(_relay.time, "time", lambda: now)
    calls = 0

    def provider() -> str:
        nonlocal calls
        calls += 1
        return _token()

    credential = RelayCredential(namespace="ns", path="path", token=provider)

    credential.resolve()
    credential.resolve()

    assert calls == 2


def test_key_credential_honours_ttl(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    credential = RelayCredential(
        namespace="ns",
        path="path",
        keyrule="rule",
        key="key",
        ttl_seconds=123,
    )

    _, claims = credential.resolve()

    assert claims["se"] == 1_800_000_123


def test_redact_relay_secrets_handles_raw_and_url_encoded_tokens():
    raw = _token()
    url = "https://ns/path?sb-hc-token=" + _relay.urllib.parse.quote(raw)

    assert raw not in redact_relay_secrets(f"failed {raw}")
    redacted_url = redact_relay_secrets(f"failed {url}")
    assert "sig%3D" not in redacted_url
    assert "sb-hc-token=<redacted>" in redacted_url
