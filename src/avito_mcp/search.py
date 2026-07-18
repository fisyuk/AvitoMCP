from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag


class AvitoError(RuntimeError):
    pass


class AvitoBlockedError(AvitoError):
    pass


class AvitoMarkupError(AvitoError):
    pass


@dataclass(frozen=True, slots=True)
class Listing:
    id: str
    title: str
    price_rub: int | None
    url: str
    location: str | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


def normalize_query(query: str) -> str:
    normalized = " ".join(query.split()).strip()
    if not normalized:
        raise ValueError("query must not be empty")
    if len(normalized) > 300:
        raise ValueError("query must not exceed 300 characters")
    return normalized


def validate_search_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.avito.ru"
        or parsed.username
        or parsed.password
    ):
        raise ValueError("search_url must be an HTTPS URL on www.avito.ru")
    if not parsed.path.startswith("/"):
        raise ValueError("search_url has an invalid path")
    return raw_url


def build_search_url(base_url: str, query: str, max_price_rub: int | None) -> str:
    validate_search_url(base_url)
    parsed = urlsplit(base_url)
    params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"context", "q", "s", "p", "pmax"}
    ]
    params.extend((("q", query), ("s", "104")))  # 104 is Avito's newest-first sort.
    if max_price_rub is not None:
        params.append(("pmax", str(max_price_rub)))
    return urlunsplit(("https", "www.avito.ru", parsed.path or "/all", urlencode(params), ""))


def search_scope(search_url: str) -> str:
    parsed = urlsplit(search_url)
    params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"q", "s", "p", "pmax", "context"}
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(sorted(params)), ""))


def search_fingerprint(query: str, max_price_rub: int | None, scope: str) -> str:
    canonical = json.dumps(
        {
            "query": normalize_query(query).casefold(),
            "max_price_rub": max_price_rub,
            "scope": scope,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def _price_from_card(card: Tag) -> int | None:
    price_node = card.select_one("[itemprop='price'][content]")
    if isinstance(price_node, Tag) and price_node.get("content"):
        raw = str(price_node["content"])
        normalized = raw.strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
        try:
            return int(Decimal(normalized))
        except (InvalidOperation, ValueError, OverflowError):
            return None
    raw = _text(card.select_one("[data-marker='item-price'], [itemprop='price']"))
    if "бесплатно" in raw.casefold():
        return 0
    digits = re.sub(r"[^0-9]", "", raw)
    return int(digits) if digits else None


def _listing_id(card: Tag, href: str) -> str | None:
    raw = card.get("data-item-id")
    if raw:
        return str(raw)
    match = re.search(r"_([0-9]{6,})(?:\?|$)", href)
    return match.group(1) if match else None


def parse_listings(html: str, max_items: int = 100) -> list[Listing]:
    lowered = html.casefold()
    blocked_markers = (
        "captcha",
        "доступ временно ограничен",
        "подтвердите, что вы не робот",
        "проверка безопасности",
    )
    if any(marker in lowered for marker in blocked_markers):
        raise AvitoBlockedError("Avito returned an anti-bot or access-check page")

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("[data-marker='item'][data-item-id], [data-marker='item']")
    listings: list[Listing] = []
    seen: set[str] = set()
    for card in cards:
        if not isinstance(card, Tag):
            continue
        link = card.select_one("a[data-marker='item-title'], a[itemprop='url'], a[href]")
        if not isinstance(link, Tag) or not link.get("href"):
            continue
        href = str(link["href"])
        item_id = _listing_id(card, href)
        if item_id is None or item_id in seen:
            continue
        title = _text(card.select_one("[data-marker='item-title'], [itemprop='name']"))
        if not title:
            title = str(link.get("title", "")).strip()
        if not title:
            continue
        item_url = urljoin("https://www.avito.ru", href)
        if urlsplit(item_url).hostname != "www.avito.ru":
            continue
        location = _text(card.select_one("[data-marker='item-address']")) or None
        listings.append(
            Listing(
                id=item_id,
                title=title[:500],
                price_rub=_price_from_card(card),
                url=item_url,
                location=location,
            )
        )
        seen.add(item_id)
        if len(listings) >= max_items:
            break

    if listings:
        return listings
    no_results_markers = ("ничего не найдено", "объявлений не найдено", "нет объявлений")
    if any(marker in lowered for marker in no_results_markers):
        return []
    raise AvitoMarkupError("Avito page contained no recognizable listings")


class AvitoSearchClient:
    def __init__(
        self,
        *,
        timeout_seconds: int,
        max_response_bytes: int,
        max_items: int,
        proxy_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._max_response_bytes = max_response_bytes
        self._max_items = max_items
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            proxy=proxy_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
                "Cache-Control": "no-cache",
            },
        )

    async def search(
        self, base_url: str, query: str, max_price_rub: int | None
    ) -> tuple[str, list[Listing]]:
        url = build_search_url(base_url, query, max_price_rub)
        try:
            async with self._client.stream("GET", url) as response:
                if response.status_code in {401, 403, 429}:
                    raise AvitoBlockedError(
                        f"Avito rejected the request with HTTP {response.status_code}"
                    )
                if response.status_code >= 400:
                    raise AvitoError(f"Avito returned HTTP {response.status_code}")
                if urlsplit(str(response.url)).hostname != "www.avito.ru":
                    raise AvitoError("Avito redirected to an unexpected host")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self._max_response_bytes:
                        raise AvitoError("Avito response exceeded the configured size limit")
                encoding = response.encoding or "utf-8"
        except httpx.HTTPError as exc:
            raise AvitoError(f"Avito request failed: {type(exc).__name__}") from exc
        html = bytes(content).decode(encoding, errors="replace")
        return url, parse_listings(html, self._max_items)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
