import base64
import hashlib

import pytest

from avito_mcp.security import (
    InvalidToken,
    TokenService,
    validate_oauth_client,
    verify_pkce,
)


def test_token_round_trip_and_tamper_rejection() -> None:
    service = TokenService("x" * 32, "https://mcp.example", "https://mcp.example", 365)
    token, expires_in = service.issue("https://chatgpt.com/oauth/test/client.json")
    claims = service.verify(token)
    assert expires_in == 365 * 24 * 60 * 60
    assert claims["scope"] == "search:read"
    header, payload, signature = token.split(".")
    signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(InvalidToken):
        service.verify(f"{header}.{payload}.{signature}")


def test_pkce_s256() -> None:
    verifier = "a-valid-verifier-with-at-least-forty-three-characters"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert verify_pkce(verifier, challenge)
    assert not verify_pkce("wrong", challenge)


def test_only_chatgpt_oauth_redirect_is_allowed_in_production() -> None:
    assert validate_oauth_client(
        "https://chatgpt.com/oauth/server/client.json",
        "https://chatgpt.com/connector/oauth/callback-id",
        development=False,
    )
    assert not validate_oauth_client(
        "https://evil.example/client.json",
        "https://evil.example/callback",
        development=False,
    )
