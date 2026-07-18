from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from avito_mcp.search import (
    AvitoBlockedError,
    AvitoSearchClient,
    build_search_url,
    parse_listings,
    search_fingerprint,
    search_scope,
    validate_search_url,
)


class StubResponse:
    def __init__(
        self,
        status_code: int,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.url = "https://www.avito.ru/all/knigi_i_zhurnaly"
        self.content = content
        self.encoding = "utf-8"
        self.headers = headers or {}


class StubClient:
    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = iter(responses)
        self.calls = 0
        self.closed = False

    async def get(self, url: str, **kwargs: object) -> StubResponse:
        self.calls += 1
        return next(self.responses)

    async def close(self) -> None:
        self.closed = True


def fixture_html_bytes() -> bytes:
    return Path("tests/fixtures/avito_search.html").read_bytes()


def test_build_search_url_preserves_category_and_replaces_search_parameters() -> None:
    result = build_search_url(
        "https://www.avito.ru/all/knigi_i_zhurnaly?context=opaque&foo=bar&q=old&p=2",
        "гигантская груша",
        2000,
    )
    parsed = urlsplit(result)
    params = parse_qs(parsed.query)
    assert parsed.path == "/all/knigi_i_zhurnaly"
    assert params == {"foo": ["bar"], "q": ["гигантская груша"], "s": ["104"], "pmax": ["2000"]}


def test_validate_search_url_rejects_other_hosts_and_credentials() -> None:
    with pytest.raises(ValueError):
        validate_search_url("https://example.com/all")
    credentialed_url = "https://" + "user" + ":" + "pass" + "@www.avito.ru/all"
    with pytest.raises(ValueError):
        validate_search_url(credentialed_url)


def test_parse_listings() -> None:
    html = Path("tests/fixtures/avito_search.html").read_text()
    items = parse_listings(html)
    assert [item.id for item in items] == ["1000001", "1000002", "1000003"]
    assert items[0].price_rub == 1500
    assert items[0].location == "Москва"
    assert items[1].price_rub == 2500
    assert items[2].price_rub == 0
    assert items[0].url == "https://www.avito.ru/all/knigi_i_zhurnaly/grusha_1000001"


def test_parse_listings_detects_block_page() -> None:
    with pytest.raises(AvitoBlockedError):
        parse_listings("<html><body>Подтвердите, что вы не робот</body></html>")


def test_scope_and_fingerprint_ignore_transient_parameters() -> None:
    first = "https://www.avito.ru/all/knigi?context=a&q=one&s=104&pmax=2000&foo=x"
    second = "https://www.avito.ru/all/knigi?context=b&q=two&s=1&pmax=3000&foo=x"
    assert search_scope(first) == search_scope(second)
    assert search_fingerprint("  Книга  ", 2000, search_scope(first)) == search_fingerprint(
        "книга", 2000, search_scope(second)
    )


async def test_search_retries_qrator_rejection_in_same_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = fixture_html_bytes()
    client = StubClient([StubResponse(429), StubResponse(200, html)])
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("avito_mcp.search.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "avito_mcp.search.random.uniform", lambda low, high: (low + high) / 2
    )
    avito = AvitoSearchClient(
        timeout_seconds=20,
        max_response_bytes=6_000_000,
        max_items=100,
        client=client,
    )

    _, listings = await avito.search(
        "https://www.avito.ru/all/knigi_i_zhurnaly", "груша", 2000
    )

    assert client.calls == 2
    assert delays == [1.0]
    assert [listing.id for listing in listings] == ["1000001", "1000002", "1000003"]


async def test_search_reports_fourth_qrator_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StubClient([StubResponse(429) for _ in range(4)])
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("avito_mcp.search.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "avito_mcp.search.random.uniform", lambda low, high: (low + high) / 2
    )
    avito = AvitoSearchClient(
        timeout_seconds=20,
        max_response_bytes=6_000_000,
        max_items=100,
        client=client,
    )

    with pytest.raises(AvitoBlockedError, match="HTTP 429"):
        await avito.search("https://www.avito.ru/all", "груша", None)

    assert client.calls == 4
    assert delays == [1.0, 2.0, 4.0]


async def test_search_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    html = fixture_html_bytes()
    client = StubClient(
        [StubResponse(429, headers={"Retry-After": "3"}), StubResponse(200, html)]
    )
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("avito_mcp.search.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "avito_mcp.search.random.uniform", lambda low, high: (low + high) / 2
    )
    avito = AvitoSearchClient(
        timeout_seconds=20,
        max_response_bytes=6_000_000,
        max_items=100,
        client=client,
    )

    _, listings = await avito.search("https://www.avito.ru/all", "груша", None)

    assert client.calls == 2
    assert delays == [3.0]
    assert len(listings) == 3


async def test_search_rotates_to_a_clean_profile_after_active_profile_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = fixture_html_bytes()
    sessions = [
        StubClient([StubResponse(429) for _ in range(4)]),
        StubClient([StubResponse(429)]),
        StubClient([StubResponse(200, html)]),
    ]
    created: list[dict[str, object]] = []

    def fake_session(**kwargs: object) -> StubClient:
        created.append(kwargs)
        return sessions[len(created) - 1]

    async def fake_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("avito_mcp.search.AsyncSession", fake_session)
    monkeypatch.setattr("avito_mcp.search.asyncio.sleep", fake_sleep)
    avito = AvitoSearchClient(
        timeout_seconds=20,
        max_response_bytes=6_000_000,
        max_items=100,
    )

    _, listings = await avito.search("https://www.avito.ru/all", "груша", None)

    assert [options["impersonate"] for options in created] == [
        "chrome146",
        "firefox147",
        "chrome145",
    ]
    assert [session.calls for session in sessions] == [4, 1, 1]
    assert sessions[0].closed is True
    assert sessions[1].closed is True
    assert sessions[2].closed is False
    assert "Brave" in str(created[2]["headers"])
    assert len(listings) == 3

    await avito.close()
    assert sessions[2].closed is True


async def test_search_rotates_after_http_200_access_check_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = fixture_html_bytes()
    sessions = [
        StubClient(
            [StubResponse(200, "Подтвердите, что вы не робот".encode())]
        ),
        StubClient([StubResponse(200, html)]),
    ]
    created: list[dict[str, object]] = []

    def fake_session(**kwargs: object) -> StubClient:
        created.append(kwargs)
        return sessions[len(created) - 1]

    monkeypatch.setattr("avito_mcp.search.AsyncSession", fake_session)
    avito = AvitoSearchClient(
        timeout_seconds=20,
        max_response_bytes=6_000_000,
        max_items=100,
    )

    _, listings = await avito.search("https://www.avito.ru/all", "груша", None)

    assert [options["impersonate"] for options in created] == [
        "chrome146",
        "firefox147",
    ]
    assert sessions[0].closed is True
    assert len(listings) == 3

    await avito.close()


async def test_search_reports_failure_only_after_every_browser_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = [
        StubClient([StubResponse(429) for _ in range(4)]),
        *[StubClient([StubResponse(429)]) for _ in range(4)],
    ]
    created: list[dict[str, object]] = []

    def fake_session(**kwargs: object) -> StubClient:
        created.append(kwargs)
        return sessions[len(created) - 1]

    async def fake_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("avito_mcp.search.AsyncSession", fake_session)
    monkeypatch.setattr("avito_mcp.search.asyncio.sleep", fake_sleep)
    avito = AvitoSearchClient(
        timeout_seconds=20,
        max_response_bytes=6_000_000,
        max_items=100,
    )

    with pytest.raises(AvitoBlockedError) as error:
        await avito.search("https://www.avito.ru/all", "груша", None)

    assert [options["impersonate"] for options in created] == [
        "chrome146",
        "firefox147",
        "chrome145",
        "safari2601",
        "chrome142",
    ]
    assert [session.calls for session in sessions] == [4, 1, 1, 1, 1]
    assert all(session.closed for session in sessions[:-1])
    assert sessions[-1].closed is False
    assert "Opera" in str(created[-1]["headers"])
    assert "chrome, mozilla-firefox, brave-chromium, safari, opera-chromium" in str(
        error.value
    )

    await avito.close()
