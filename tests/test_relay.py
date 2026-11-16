from urllib.parse import parse_qs, urlsplit

import pytest

from privy import _relay
from privy._relay import (
    RelayCredential,
    RelayTokenAudienceError,
    RelayTokenError,
    RelayTokenExpiredError,
    create_sas_token,
    mint_relay_sas,
    parse_relay_connection_string,
    parse_sas_token,
    redact_relay_secrets,
    validate_sas_token,
)

CONNECTION = (
    "Endpoint=sb://contoso.servicebus.windows.net/;"
    "SharedAccessKeyName=Send;SharedAccessKey=c3ludGhldGljLWtleQ=="
)


def test_connection_string_preserves_padded_key_and_scopes_one_mint(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    parsed = parse_relay_connection_string(CONNECTION + ";EntityPath=my-hyco;")
    assert parsed.key == "c3ludGhldGljLWtleQ=="
    assert parsed.key not in repr(parsed)
    result = mint_relay_sas(CONNECTION + ";EntityPath=my-hyco")
    expected = create_sas_token("contoso", "my-hyco", "Send", parsed.key, 21600)
    assert result["token"] == expected
    claims = parse_sas_token(result["token"])
    assert claims["se"] == 1_800_021_600
    assert claims["sr"] == "http://contoso.servicebus.windows.net/my-hyco"
    assert result["expires_utc"] == _relay._format_utc(claims["se"])
    url = urlsplit(result["sender_url"])
    assert url.scheme == "https" and url.path == "/my-hyco"
    assert parse_qs(url.query) == {"sb-hc-token": [expected]}


def test_connection_string_requires_entity_or_matching_fallback(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    result = mint_relay_sas(CONNECTION, "nested/hyco", 90)
    assert result["expires_at"] == 1_800_000_090
    assert parse_sas_token(result["token"])["sr"].endswith("/nested/hyco")
    with pytest.raises(ValueError, match="specific Hybrid Connection"):
        mint_relay_sas(CONNECTION)
    with pytest.raises(ValueError, match="conflicts"):
        mint_relay_sas(CONNECTION + ";EntityPath=one", "two")


@pytest.mark.parametrize(
    "suffix",
    [";EntityPath=../x", ";EntityPath=/", ";EntityPath=x?y", ";EntityPath=x#y", ";EntityPath=x%2fy"],
)
def test_mint_rejects_ambiguous_entity_paths(suffix):
    with pytest.raises(ValueError, match="Hybrid Connection"):
        mint_relay_sas(CONNECTION + suffix)


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "not-a-connection-string",
        CONNECTION + ";SharedAccessKey=other",
        CONNECTION + ";sharedaccesskey=other",
        CONNECTION + ";EntityPath=",
        CONNECTION.replace("sb://", "https://"),
        CONNECTION.replace("contoso.servicebus.windows.net/", "contoso.servicebus.windows.net/path"),
        CONNECTION.replace("contoso.servicebus.windows.net/", "user@contoso.servicebus.windows.net/"),
        CONNECTION.replace("c3ludGhldGljLWtleQ==", "not-base64"),
        CONNECTION + ";SharedAccessSignature=secret",
    ],
)
def test_connection_string_rejects_malformed_values_without_reflecting_input(value):
    with pytest.raises(ValueError) as error:
        parse_relay_connection_string(value)
    assert "c3ludGhldGljLWtleQ==" not in str(error.value)
    assert "not-base64" not in str(error.value)


@pytest.mark.parametrize("ttl", [0, -1, True, 1.5, "6h", float("nan"), float("inf"), 10**100])
def test_mint_rejects_invalid_expiry(ttl):
    with pytest.raises(ValueError, match="Lifetime"):
        mint_relay_sas(CONNECTION, "hyco", ttl)


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
