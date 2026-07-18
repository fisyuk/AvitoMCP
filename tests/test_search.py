from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from avito_mcp.search import (
    AvitoBlockedError,
    build_search_url,
    parse_listings,
    search_fingerprint,
    search_scope,
    validate_search_url,
)


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
