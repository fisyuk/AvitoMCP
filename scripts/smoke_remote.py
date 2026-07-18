from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys
from urllib.parse import parse_qs, urlsplit

import httpx


def _pkce() -> tuple[str, str]:
    verifier = "docker-smoke-pkce-verifier-with-at-least-forty-three-characters"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _rpc(client: httpx.Client, token: str, request_id: int, method: str, params: dict) -> dict:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(f"MCP {method} failed: {payload['error']}")
    return payload["result"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a local development Avito MCP server")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765")
    parser.add_argument("--public-base-url", default="http://localhost:8765")
    parser.add_argument("--access-code", required=True)
    parser.add_argument("--query", default="книга невероятная история о гигантской груше")
    parser.add_argument("--max-price", type=int, default=2000)
    args = parser.parse_args()

    verifier, challenge = _pkce()
    host = urlsplit(args.public_base_url).netloc
    client_id = "https://chatgpt.com"
    redirect_uri = "https://chatgpt.com/connector/oauth/avito-mcp-smoke"

    with httpx.Client(
        base_url=args.endpoint,
        headers={"Host": host},
        follow_redirects=False,
        timeout=30,
    ) as client:
        health = client.get("/healthz")
        health.raise_for_status()
        print("health=ok")

        authorize = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": "docker-smoke",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": args.public_base_url,
                "scope": "search:read",
            },
        )
        authorize.raise_for_status()
        match = re.search(r'name="transaction_id" value="([^"]+)"', authorize.text)
        if match is None:
            raise RuntimeError("OAuth login page did not contain a transaction ID")
        approval = client.post(
            "/authorize",
            data={"transaction_id": match.group(1), "access_code": args.access_code},
        )
        if approval.status_code != 303:
            raise RuntimeError(f"OAuth approval failed with HTTP {approval.status_code}")
        code = parse_qs(urlsplit(approval.headers["location"]).query)["code"][0]
        token_response = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "resource": args.public_base_url,
            },
        )
        token_response.raise_for_status()
        token = token_response.json()["access_token"]
        print("oauth=ok")

        initialized = _rpc(
            client,
            token,
            1,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "docker-smoke", "version": "1"},
            },
        )
        print(f"mcp_initialize=ok server={initialized['serverInfo']['name']}")
        tools = _rpc(client, token, 2, "tools/list", {})
        tool_names = [tool["name"] for tool in tools["tools"]]
        print(f"tools={','.join(tool_names)}")

        result = _rpc(
            client,
            token,
            3,
            "tools/call",
            {
                "name": "search_avito",
                "arguments": {
                    "query": args.query,
                    "max_price_rub": args.max_price,
                    "only_new": True,
                },
            },
        )
        if result.get("isError"):
            messages = [item.get("text", "") for item in result.get("content", [])]
            print(f"search_avito=error detail={' '.join(messages)[:500]}")
            return 2
        structured = result.get("structuredContent", {})
        print(
            "search_avito=ok "
            f"eligible={structured.get('eligible_count')} new={structured.get('new_count')}"
        )
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"smoke=failed detail={exc}", file=sys.stderr)
        raise SystemExit(1) from exc

