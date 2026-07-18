from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    public_base_url: str
    access_code: str
    token_secret: str
    database_path: Path
    avito_default_search_url: str
    avito_proxy_url: str | None = None
    token_ttl_days: int = 365
    request_timeout_seconds: int = 20
    max_response_bytes: int = 6_000_000
    max_items: int = 100
    port: int = 8000
    development_oauth: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        base_url = _required("PUBLIC_BASE_URL").rstrip("/")
        parsed = urlsplit(base_url)
        development = os.getenv("DEVELOPMENT_OAUTH", "").lower() in {"1", "true", "yes"}
        local_http = (
            development
            and parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1"}
        )
        if parsed.scheme != "https" and not local_http:
            raise RuntimeError("PUBLIC_BASE_URL must be HTTPS (localhost HTTP is development-only)")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise RuntimeError("PUBLIC_BASE_URL must contain only scheme and host")

        access_code = _required("ACCESS_CODE")
        token_secret = _required("TOKEN_SECRET")
        if len(access_code) < 16:
            raise RuntimeError("ACCESS_CODE must be at least 16 characters")
        if len(token_secret.encode()) < 32:
            raise RuntimeError("TOKEN_SECRET must be at least 32 bytes")

        proxy_url = os.getenv("AVITO_PROXY_URL") or None
        if proxy_url:
            proxy = urlsplit(proxy_url)
            if proxy.scheme not in {"http", "https"} or not proxy.hostname:
                raise RuntimeError("AVITO_PROXY_URL must be a valid HTTP(S) proxy URL")

        return cls(
            public_base_url=base_url,
            access_code=access_code,
            token_secret=token_secret,
            database_path=Path(os.getenv("DATABASE_PATH", "/data/history.sqlite3")),
            avito_default_search_url=os.getenv(
                "AVITO_DEFAULT_SEARCH_URL", "https://www.avito.ru/all"
            ),
            avito_proxy_url=proxy_url,
            token_ttl_days=_positive_int("TOKEN_TTL_DAYS", 365),
            request_timeout_seconds=_positive_int("REQUEST_TIMEOUT_SECONDS", 20),
            max_response_bytes=_positive_int("MAX_RESPONSE_BYTES", 6_000_000),
            max_items=_positive_int("MAX_ITEMS", 100),
            port=_positive_int("PORT", 8000),
            development_oauth=development,
        )
