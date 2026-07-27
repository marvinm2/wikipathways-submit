"""GitHub App (bot) identity: JWT signing, installation-token caching, and the mirror comment.

These exercise the real code paths with an injected ``httpx.MockTransport`` (no network) and a
throwaway RSA key, so the App-auth handshake and the read-only mirror-comment upsert are covered
without a live GitHub App.
"""
from __future__ import annotations

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import GitHubApp, GitHubAppError
from app.github import GitHubError, HttpGitHubClient


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def test_installation_token_signs_valid_jwt_and_caches(rsa_keypair):
    private_pem, public_pem = rsa_keypair
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/42/access_tokens"
        assert request.method == "POST"
        bearer = request.headers["authorization"].removeprefix("Bearer ")
        # The App JWT must verify against the App's public key and carry the App id as issuer.
        # exp is relative to the injected fake clock, so don't validate it against wall time.
        decoded = jwt.decode(
            bearer, public_pem, algorithms=["RS256"], options={"verify_exp": False}
        )
        assert decoded["iss"] == "app-id-123"
        calls.append(bearer)
        return httpx.Response(201, json={"token": "ghs_installtoken"})

    app = GitHubApp(
        "app-id-123", private_pem, "42", transport=httpx.MockTransport(handler)
    )

    # Fixed clock so caching is deterministic.
    assert app.installation_token(now=1000.0) == "ghs_installtoken"
    assert app.installation_token(now=1200.0) == "ghs_installtoken"  # cached — no second exchange
    assert len(calls) == 1

    # Past the ~1h expiry it refreshes.
    assert app.installation_token(now=1000.0 + 3600) == "ghs_installtoken"
    assert len(calls) == 2


def test_installation_token_raises_on_github_error(rsa_keypair):
    private_pem, _ = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad credentials"})

    app = GitHubApp("id", private_pem, "42", transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubAppError):
        app.installation_token(now=1000.0)


def test_installation_token_raises_on_bad_private_key():
    app = GitHubApp("id", "-----BEGIN PRIVATE KEY-----\nnot-a-key\n", "42")
    with pytest.raises(GitHubAppError):
        app.installation_token(now=1000.0)


def test_upsert_issue_comment_creates_then_updates():
    """First upsert has no marker comment → POST; second finds it → PATCH the same comment."""
    store: dict[int, str] = {}
    next_id = [100]
    marker = "<!-- wikipathways-submit:mirror -->"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/comments"):
            return httpx.Response(
                200, json=[{"id": cid, "body": body} for cid, body in store.items()]
            )
        if request.method == "POST" and path.endswith("/comments"):
            cid = next_id[0]
            next_id[0] += 1
            store[cid] = request.read().decode()  # crude: body includes the marker text
            return httpx.Response(201, json={"id": cid})
        if request.method == "PATCH" and "/comments/" in path:
            cid = int(path.rsplit("/", 1)[1])
            store[cid] = request.read().decode()
            return httpx.Response(200, json={"id": cid})
        return httpx.Response(404)

    client = HttpGitHubClient("tok", transport=httpx.MockTransport(handler))
    repo = "wikipathways/wikipathways-database"

    client.upsert_issue_comment(repo, 7, f"{marker}\nfirst", marker=marker)
    assert len(store) == 1  # created
    client.upsert_issue_comment(repo, 7, f"{marker}\nsecond", marker=marker)
    assert len(store) == 1  # updated in place, not duplicated
    assert "second" in next(iter(store.values()))


def test_upsert_issue_comment_surfaces_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = HttpGitHubClient("tok", transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubError):
        client.upsert_issue_comment("o/r", 1, "body", marker="m")


def test_an_unreadable_private_key_disables_the_bot_instead_of_crashing(tmp_path):
    # A deployment that sets the key path before the secret exists must degrade to "no bot"
    # (503 on the routes that need it), not crash-loop on startup with the site already live.
    from app.config import Settings
    from app.main import _make_bot_app

    settings = Settings(
        _env_file=None,
        github_app_id="4403728",
        github_app_installation_id="149294202",
        github_app_private_key_path=str(tmp_path / "definitely-not-there.pem"),
    )

    assert _make_bot_app(settings) is None
