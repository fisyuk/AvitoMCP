import base64
import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

from avito_mcp.config import Settings
from avito_mcp.search import Listing, build_search_url
from avito_mcp.server import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        public_base_url="http://localhost:8000",
        access_code="test-access-code-long-enough",
        token_secret="t" * 32,
        database_path=tmp_path / "history.sqlite3",
        avito_default_search_url="https://www.avito.ru/all/knigi_i_zhurnaly",
        development_oauth=True,
    )


class FakeAvitoClient:
    def __init__(self) -> None:
        self.calls = 0

    async def search(
        self, base_url: str, query: str, max_price_rub: int | None
    ) -> tuple[str, list[Listing]]:
        self.calls += 1
        items = [
            Listing("1", "Matching book", 1500, "https://www.avito.ru/item_1"),
            Listing("2", "Too expensive", 2500, "https://www.avito.ru/item_2"),
        ]
        if self.calls > 1:
            items.append(Listing("3", "New book", 1800, "https://www.avito.ru/item_3"))
        return build_search_url(base_url, query, max_price_rub), items

    async def close(self) -> None:
        return None


def test_oauth_and_authenticated_mcp_initialize(tmp_path: Path) -> None:
    fake_avito = FakeAvitoClient()
    app = create_app(_settings(tmp_path), avito_client=fake_avito)
    verifier = "test-pkce-verifier-with-at-least-forty-three-characters"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    client_id = "http://localhost/client"
    redirect_uri = "http://localhost/callback"
    resource = "http://localhost:8000"

    with TestClient(app) as client:
        unauthorized = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Host": "localhost:8000",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert unauthorized.status_code == 401
        assert "resource_metadata" in unauthorized.headers["www-authenticate"]

        authorize = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": "state-1",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": resource,
                "scope": "search:read",
            },
        )
        transaction_id = re.search(
            r'name="transaction_id" value="([^"]+)"', authorize.text
        ).group(1)
        approval = client.post(
            "/authorize",
            data={
                "transaction_id": transaction_id,
                "access_code": "test-access-code-long-enough",
            },
            follow_redirects=False,
        )
        assert approval.status_code == 303
        code = parse_qs(urlsplit(approval.headers["location"]).query)["code"][0]
        token_response = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "resource": resource,
            },
        )
        token = token_response.json()["access_token"]

        initialized = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Host": "localhost:8000",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "Avito Monitor"

        tools = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Host": "localhost:8000",
            },
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )
        tool = tools.json()["result"]["tools"][0]
        assert tool["name"] == "search_avito"
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["_meta"]["securitySchemes"][0]["type"] == "oauth2"

        def call_search(request_id: int) -> dict:
            response = client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                    "Host": "localhost:8000",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "search_avito",
                        "arguments": {
                            "query": "книга",
                            "max_price_rub": 2000,
                            "only_new": True,
                        },
                    },
                },
            )
            assert response.status_code == 200
            return response.json()["result"]["structuredContent"]

        first = call_search(4)
        second = call_search(5)
        assert first["initial_run"] is True
        assert [item["id"] for item in first["items"]] == ["1"]
        assert second["initial_run"] is False
        assert [item["id"] for item in second["items"]] == ["3"]
