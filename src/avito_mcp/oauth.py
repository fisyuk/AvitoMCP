from __future__ import annotations

import html
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .config import Settings
from .security import (
    AuthorizationRequest,
    OAuthStateStore,
    TokenService,
    validate_oauth_client,
    verify_access_code,
    verify_pkce,
)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _oauth_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return _no_store(
        JSONResponse(
            {"error": error, "error_description": description}, status_code=status_code
        )
    )


def _login_page(transaction_id: str, error: str | None = None) -> HTMLResponse:
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Подключение Avito Monitor</title>
  <style>
    body {{
      font: 16px/1.45 system-ui, sans-serif; max-width: 420px;
      margin: 12vh auto; padding: 0 20px; color: #111;
    }}
    h1 {{ font-size: 24px; }}
    label {{ display: block; margin: 24px 0 8px; }}
    input {{ box-sizing: border-box; width: 100%; padding: 12px; font: inherit; }}
    button {{
      margin-top: 16px; width: 100%; padding: 12px;
      font: inherit; font-weight: 650; cursor: pointer;
    }}
    .error {{ color: #b42318; }}
    .hint {{ color: #555; font-size: 14px; }}
  </style>
</head>
<body>
  <h1>Подключение Avito Monitor</h1>
  <p>Введите код доступа, заданный владельцем сервера.</p>
  {error_html}
  <form method="post" action="/authorize">
    <input type="hidden" name="transaction_id" value="{html.escape(transaction_id)}">
    <label for="access_code">Код доступа</label>
    <input id="access_code" name="access_code" type="password"
           required autofocus autocomplete="current-password">
    <button type="submit">Подключить</button>
  </form>
  <p class="hint">Код не сохраняется. ChatGPT получит bearer-токен для вызова только поиска.</p>
</body>
</html>"""
    return _no_store(HTMLResponse(page))


async def _read_form(request: Request, max_bytes: int) -> dict[str, list[str]] | None:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                return None
        except ValueError:
            return None
    body = await request.body()
    if len(body) > max_bytes:
        return None
    return parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20)


class OAuthEndpoints:
    def __init__(self, settings: Settings, tokens: TokenService, state: OAuthStateStore) -> None:
        self.settings = settings
        self.tokens = tokens
        self.state = state

    async def authorization_metadata(self, request: Request) -> JSONResponse:
        base = self.settings.public_base_url
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/oauth/token",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["search:read"],
                "client_id_metadata_document_supported": True,
            }
        )

    async def protected_resource_metadata(self, request: Request) -> JSONResponse:
        base = self.settings.public_base_url
        return JSONResponse(
            {
                "resource": base,
                "authorization_servers": [base],
                "scopes_supported": ["search:read"],
                "resource_documentation": f"{base}/privacy",
            }
        )

    async def authorize_get(self, request: Request) -> Response:
        params = request.query_params
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        challenge = params.get("code_challenge", "")
        resource = params.get("resource", self.settings.public_base_url)
        scope = params.get("scope", "search:read")
        if any(
            len(value) > limit
            for value, limit in (
                (client_id, 2048),
                (redirect_uri, 2048),
                (state, 2048),
                (challenge, 128),
                (scope, 256),
            )
        ):
            return _oauth_error("invalid_request", "OAuth parameter is too long")
        if params.get("response_type") != "code":
            return _oauth_error("unsupported_response_type", "Only authorization code is supported")
        if params.get("code_challenge_method") != "S256" or not 43 <= len(challenge) <= 128:
            return _oauth_error("invalid_request", "PKCE S256 is required")
        if not state:
            return _oauth_error("invalid_request", "state is required")
        if resource != self.settings.public_base_url:
            return _oauth_error("invalid_target", "Unexpected OAuth resource")
        if "search:read" not in scope.split():
            return _oauth_error("invalid_scope", "search:read is required")
        if not validate_oauth_client(client_id, redirect_uri, self.settings.development_oauth):
            return _oauth_error("invalid_client", "Only the ChatGPT OAuth client is allowed", 401)

        transaction_id = self.state.create_request(
            AuthorizationRequest(
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=challenge,
                resource=resource,
                scope="search:read",
                expires_at=time.time() + 600,
            )
        )
        return _login_page(transaction_id)

    async def authorize_post(self, request: Request) -> Response:
        form = await _read_form(request, 4_096)
        if form is None:
            return _oauth_error("invalid_request", "Request body is too large", 413)
        transaction_id = form.get("transaction_id", [""])[0]
        supplied_code = form.get("access_code", [""])[0]
        if not transaction_id:
            return _oauth_error("invalid_request", "Missing authorization transaction")
        if not verify_access_code(self.settings.access_code, supplied_code):
            return _login_page(transaction_id, "Неверный код доступа")
        approved = self.state.approve_request(transaction_id)
        if approved is None:
            return _oauth_error("invalid_request", "Authorization transaction expired")
        code, auth_request = approved
        separator = "&" if urlsplit(auth_request.redirect_uri).query else "?"
        result_params = urlencode({"code": code, "state": auth_request.state})
        target = f"{auth_request.redirect_uri}{separator}{result_params}"
        return _no_store(RedirectResponse(target, status_code=303))

    async def token(self, request: Request) -> Response:
        form = await _read_form(request, 8_192)
        if form is None:
            return _oauth_error("invalid_request", "Request body is too large", 413)
        if form.get("grant_type", [""])[0] != "authorization_code":
            return _oauth_error("unsupported_grant_type", "Only authorization_code is supported")
        code = form.get("code", [""])[0]
        verifier = form.get("code_verifier", [""])[0]
        redirect_uri = form.get("redirect_uri", [""])[0]
        client_id = form.get("client_id", [""])[0]
        resource = form.get("resource", [self.settings.public_base_url])[0]
        authorization = self.state.consume_code(code)
        if authorization is None:
            return _oauth_error("invalid_grant", "Authorization code is invalid or expired")
        if (
            not 43 <= len(verifier) <= 128
            or authorization.redirect_uri != redirect_uri
            or authorization.client_id != client_id
            or authorization.resource != resource
            or not verify_pkce(verifier, authorization.code_challenge)
        ):
            return _oauth_error("invalid_grant", "Authorization code validation failed")
        access_token, expires_in = self.tokens.issue(client_id, authorization.scope)
        return _no_store(
            JSONResponse(
                {
                    "access_token": access_token,
                    "token_type": "Bearer",
                    "expires_in": expires_in,
                    "scope": authorization.scope,
                }
            )
        )
