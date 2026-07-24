"""GitHub OAuth Authorization Code flow (per-user identity).

The submitter authorizes the app once; the app receives a user access token and acts *as them*
for the branch push / PR open, so authorship is real and the user never runs git (design §4.1).

The three steps:

1. ``authorize_url`` — where to send the browser to grant access.
2. ``exchange_code`` — trade the returned ``?code`` for a user access token.
3. ``get_login`` — resolve the token to a GitHub username (the stored identity).

An ``httpx.MockTransport`` can be injected so the flow is unit-tested without hitting GitHub.
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"


class OAuthError(RuntimeError):
    """The OAuth exchange failed."""


class GithubOAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport  # tests inject httpx.MockTransport

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, timeout=15.0)

    def authorize_url(self, redirect_uri: str, state: str, scope: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "allow_signup": "false",
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> str:
        with self._client() as client:
            resp = client.post(
                _TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
        if resp.is_error:
            raise OAuthError(f"token exchange failed: {resp.status_code}")
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise OAuthError(f"no access_token in response: {payload.get('error', payload)}")
        return token

    def get_login(self, token: str) -> str:
        with self._client() as client:
            resp = client.get(
                _USER_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        if resp.is_error:
            raise OAuthError(f"could not resolve user: {resp.status_code}")
        login = resp.json().get("login")
        if not login:
            raise OAuthError("user response had no login")
        return login
