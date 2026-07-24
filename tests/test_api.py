from __future__ import annotations

import io

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import GithubOAuth
from app.config import Settings
from app.github import FakeGitHubClient
from app.main import build_app, get_current_user, get_github_client

GOOD_GPML = (
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    b'Organism="Homo sapiens" Version="WP5636_r20260520113005"></Pathway>'
)
BAD_GPML = b"<html>not a pathway</html>"
REV_GPML = (
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    b'Organism="Homo sapiens" Version="WP5636_r19990101000000"></Pathway>'
)


def _settings(**kw):
    # _env_file=None keeps tests hermetic from the developer's local .env.
    kw.setdefault("dev_wpid_floor", 5636)
    return Settings(_env_file=None, **kw)


@pytest.fixture
def client(tmp_path):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    with TestClient(build_app(settings)) as c:
        yield c


def _authed_app(tmp_path, *, curators=(), fake=None):
    """Build an app with GitHub + identity dependencies overridden.

    Returns (app, current) where ``current`` is a mutable dict; set ``current['user']`` to change
    who the session identity resolves to between requests.
    """
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        curators=list(curators),
    )
    app = build_app(settings)
    fake = fake or FakeGitHubClient(
        default_branches={f"{settings.content_repo}#{settings.default_branch}": "basesha"}
    )
    current = {"user": "alice"}
    app.dependency_overrides[get_github_client] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.state._fake = fake  # for assertions
    return app, current


# -- read-only endpoints (no auth) ---------------------------------------------------------


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_validate_good_gpml(client):
    resp = client.post(
        "/api/validate",
        files={"file": ("upload.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["organism"] == "Homo sapiens"
    assert body["embedded_wpid"] == "WP5636"


def test_validate_rejects_bad_gpml(client):
    resp = client.post(
        "/api/validate",
        files={"file": ("upload.gpml", io.BytesIO(BAD_GPML), "application/xml")},
    )
    assert resp.status_code == 422


# -- auth gating ---------------------------------------------------------------------------


def test_submit_requires_auth(client):
    # No session → 401 (not 503): the app is configured, the caller just isn't logged in.
    resp = client.post(
        "/api/submit",
        files={"file": ("upload.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
    )
    assert resp.status_code == 401


def test_auth_me_anonymous(client):
    assert client.get("/auth/me").json() == {
        "authenticated": False,
        "login": None,
        "is_curator": False,
    }


# -- submit / update -----------------------------------------------------------------------


def test_submit_success(tmp_path):
    app, _current = _authed_app(tmp_path)
    with TestClient(app) as c:
        resp = c.post(
            "/api/submit",
            files={"file": ("upload.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["wpid"] == "WP5637"
    assert body["path"] == "pathways/WP5637/WP5637.gpml"


def test_update_success_lock_and_release(tmp_path):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "basesha"},
        existing_files={f"{repo}#pathways/WP5636/WP5636.gpml": "oldsha"},
    )
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    with TestClient(app) as c:
        current["user"] = "alice"
        r1 = c.post(
            "/api/pathways/5636/update",
            files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
        )
        assert r1.status_code == 201

        # A different user is now blocked (lock held by alice) → 409.
        current["user"] = "bob"
        r2 = c.post(
            "/api/pathways/5636/update",
            files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
        )
        assert r2.status_code == 409
        assert r2.json()["detail"]["held_by"] == "alice"

        # Non-curator cannot force-release (403); curator can.
        current["user"] = "bob"
        assert c.post("/api/pathways/5636/release").status_code == 403
        current["user"] = "curator"
        assert c.post("/api/pathways/5636/release").json() == {"released": True}


# -- curation dashboard --------------------------------------------------------------------


def test_dashboard_end_to_end(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]

        queue = c.get("/api/reviews").json()
        assert [r["pr_number"] for r in queue] == [pr]
        assert queue[0]["submitter"] == "bob"

        # Approving before the checklist is complete is refused (409).
        current["user"] = "curator"
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 409

        detail = c.get(f"/api/reviews/{pr}").json()
        for item in detail["checklist"]:
            if item["required"]:
                c.post(f"/api/reviews/{pr}/checklist", data={"key": item["key"], "state": "pass"})

        # A non-curator cannot approve (403).
        current["user"] = "randouser"
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 403

        # The curator approves → merges.
        current["user"] = "curator"
        ok = c.post(f"/api/reviews/{pr}/approve")
        assert ok.status_code == 200
        assert ok.json()["status"] == "merged"
        assert ok.json()["approved_by"] == "curator"
        assert pr in app.state._fake.merged
        assert c.get("/api/reviews").json() == []


# -- OAuth flow ----------------------------------------------------------------------------


def test_login_redirects_to_github(tmp_path):
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        github_oauth_client_id="cid123",
        github_oauth_client_secret="secret",
        oauth_redirect_uri="http://testserver/auth/callback",
    )
    with TestClient(build_app(settings)) as c:
        resp = c.get("/auth/login", follow_redirects=False)
        assert resp.status_code == 302
        loc = resp.headers["location"]
        assert loc.startswith("https://github.com/login/oauth/authorize")
        assert "client_id=cid123" in loc
        assert "state=" in loc


def test_login_503_when_unconfigured(client):
    assert client.get("/auth/login", follow_redirects=False).status_code == 503


def test_callback_exchanges_code_and_sets_session(tmp_path):
    # Mock GitHub's token + user endpoints so the flow runs without a network.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gho_faketoken"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "alice"})
        return httpx.Response(404)

    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        github_oauth_client_id="cid",
        github_oauth_client_secret="sec",
        oauth_redirect_uri="http://testserver/auth/callback",
        curators=["alice"],
    )
    app = build_app(settings)
    with TestClient(app) as c:
        # Inject the mock transport into the live oauth object.
        app.state.oauth = GithubOAuth("cid", "sec", transport=httpx.MockTransport(handler))
        # Seed a matching CSRF state via a login round-trip.
        login = c.get("/auth/login", follow_redirects=False)
        query = login.headers["location"].split("?", 1)[1]
        state = dict(p.split("=", 1) for p in query.split("&"))["state"]
        cb = c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
        assert cb.status_code == 302
        me = c.get("/auth/me").json()
        assert me == {"authenticated": True, "login": "alice", "is_curator": True}


def test_callback_rejects_bad_state(tmp_path):
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        github_oauth_client_id="cid",
        github_oauth_client_secret="sec",
    )
    app = build_app(settings)
    with TestClient(app) as c:
        app.state.oauth = GithubOAuth("cid", "sec")
        # No prior /auth/login → no stored state → mismatch.
        resp = c.get("/auth/callback?code=abc&state=forged", follow_redirects=False)
        assert resp.status_code == 400
