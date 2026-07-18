from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import AccessToken, TokenVerifier


class InvalidToken(ValueError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class TokenService:
    def __init__(self, secret: str, issuer: str, audience: str, ttl_days: int) -> None:
        self._secret = secret.encode()
        self.issuer = issuer
        self.audience = audience
        self.ttl_seconds = ttl_days * 24 * 60 * 60

    def issue(self, client_id: str, scope: str = "search:read") -> tuple[str, int]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": "shared-access",
            "client_id": client_id,
            "scope": scope,
            "iat": now,
            "exp": now + self.ttl_seconds,
            "jti": secrets.token_urlsafe(16),
        }
        header = {"alg": "HS256", "typ": "JWT"}
        encoded_header = _b64encode(json.dumps(header, separators=(",", ":")).encode())
        encoded_claims = _b64encode(json.dumps(claims, separators=(",", ":")).encode())
        signing_input = f"{encoded_header}.{encoded_claims}".encode()
        signature = _b64encode(hmac.new(self._secret, signing_input, hashlib.sha256).digest())
        return f"{encoded_header}.{encoded_claims}.{signature}", self.ttl_seconds

    def verify(self, token: str) -> dict[str, Any]:
        try:
            encoded_header, encoded_claims, encoded_signature = token.split(".")
            signing_input = f"{encoded_header}.{encoded_claims}".encode()
            expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
            supplied = _b64decode(encoded_signature)
            if not hmac.compare_digest(expected, supplied):
                raise InvalidToken("invalid signature")
            header = json.loads(_b64decode(encoded_header))
            claims = json.loads(_b64decode(encoded_claims))
            if not isinstance(header, dict) or not isinstance(claims, dict):
                raise InvalidToken("malformed token")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            if isinstance(exc, InvalidToken):
                raise
            raise InvalidToken("malformed token") from exc

        now = int(time.time())
        if header.get("alg") != "HS256":
            raise InvalidToken("unsupported algorithm")
        if claims.get("iss") != self.issuer or claims.get("aud") != self.audience:
            raise InvalidToken("issuer or audience mismatch")
        if not isinstance(claims.get("exp"), int) or claims["exp"] <= now:
            raise InvalidToken("expired token")
        scopes = str(claims.get("scope", "")).split()
        if "search:read" not in scopes:
            raise InvalidToken("missing scope")
        if not isinstance(claims.get("client_id"), str):
            raise InvalidToken("missing client_id")
        return claims


class SharedTokenVerifier(TokenVerifier):
    def __init__(self, tokens: TokenService) -> None:
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        if len(token) > 8_192:
            return None
        try:
            claims = self._tokens.verify(token)
        except InvalidToken:
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=str(claims["scope"]).split(),
            expires_at=claims["exp"],
            resource=claims["aud"],
        )


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    resource: str
    scope: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class AuthorizationCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    resource: str
    scope: str
    expires_at: float


class OAuthStateStore:
    """Keeps short-lived OAuth handshakes in memory; nothing auth-related is persisted."""

    def __init__(self, max_pending: int = 1_000) -> None:
        self._requests: dict[str, AuthorizationRequest] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._lock = Lock()
        self._max_pending = max_pending

    def create_request(self, request: AuthorizationRequest) -> str:
        transaction_id = secrets.token_urlsafe(32)
        with self._lock:
            self._cleanup()
            while len(self._requests) >= self._max_pending:
                oldest = min(self._requests, key=lambda key: self._requests[key].expires_at)
                self._requests.pop(oldest)
            self._requests[transaction_id] = request
        return transaction_id

    def approve_request(self, transaction_id: str) -> tuple[str, AuthorizationRequest] | None:
        with self._lock:
            self._cleanup()
            request = self._requests.pop(transaction_id, None)
            if request is None:
                return None
            code = secrets.token_urlsafe(32)
            self._codes[code] = AuthorizationCode(
                client_id=request.client_id,
                redirect_uri=request.redirect_uri,
                code_challenge=request.code_challenge,
                resource=request.resource,
                scope=request.scope,
                expires_at=time.time() + 300,
            )
            return code, request

    def consume_code(self, code: str) -> AuthorizationCode | None:
        with self._lock:
            self._cleanup()
            return self._codes.pop(code, None)

    def _cleanup(self) -> None:
        now = time.time()
        self._requests = {
            key: value for key, value in self._requests.items() if value.expires_at > now
        }
        self._codes = {key: value for key, value in self._codes.items() if value.expires_at > now}


def verify_access_code(expected: str, supplied: str) -> bool:
    return hmac.compare_digest(expected.encode(), supplied.encode())


def verify_pkce(verifier: str, challenge: str) -> bool:
    calculated = _b64encode(hashlib.sha256(verifier.encode()).digest())
    return hmac.compare_digest(calculated, challenge)


def validate_oauth_client(client_id: str, redirect_uri: str, development: bool) -> bool:
    client = urlsplit(client_id)
    redirect = urlsplit(redirect_uri)
    local_redirect = (
        development
        and redirect.scheme == "http"
        and redirect.hostname in {"localhost", "127.0.0.1"}
    )
    if local_redirect:
        return True
    return (
        client.scheme == "https"
        and client.hostname == "chatgpt.com"
        and redirect.scheme == "https"
        and redirect.hostname == "chatgpt.com"
        and (
            redirect.path.startswith("/connector/oauth/")
            or redirect.path == "/connector_platform_oauth_redirect"
        )
    )
