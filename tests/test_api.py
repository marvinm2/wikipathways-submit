from __future__ import annotations

import io

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import GithubOAuth
from app.config import Settings
from app.github import FakeGitHubClient
from app.main import (
    build_app,
    get_bot_client,
    get_bot_optional,
    get_current_user,
    get_github_client,
)

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


def _authed_app(tmp_path, *, curators=(), fake=None, webhook_secret=None):
    """Build an app with GitHub + identity dependencies overridden.

    Returns (app, current) where ``current`` is a mutable dict; set ``current['user']`` to change
    who the session identity resolves to between requests.
    """
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        curators=list(curators),
        github_webhook_secret=webhook_secret,
        preview_cache_dir=str(tmp_path / "preview-cache"),
    )
    app = build_app(settings)
    fake = fake or FakeGitHubClient(
        default_branches={f"{settings.content_repo}#{settings.default_branch}": "basesha"}
    )
    current = {"user": "alice"}
    app.dependency_overrides[get_github_client] = lambda: fake
    # The same fake stands in for the bot (App) identity — merge + mirror comment run through it,
    # so ``fake.merged`` / ``fake.comments`` capture the privileged actions.
    app.dependency_overrides[get_bot_optional] = lambda: fake
    app.dependency_overrides[get_bot_client] = lambda: fake
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
            data={"description": "Curated from Reactome; please check the HGNC ids."},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["wpid"] == "WP5637"
    assert body["path"] == "pathways/WP5637/WP5637.gpml"
    # The submitter note travels through the Form field into the PR body.
    pr_body = app.state._fake.pull_meta[body["pr_number"]]["body"]
    assert "**Note from the submitter**" in pr_body
    assert "Curated from Reactome" in pr_body


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


def test_pathway_info_reports_presence(tmp_path):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "base"},
        existing_contents={f"{repo}#pathways/WP5636/WP5636.gpml": GOOD_GPML.decode()},
    )
    app, _current = _authed_app(tmp_path, fake=fake)
    with TestClient(app) as c:
        found = c.get("/api/pathways/5636").json()
        assert found["exists"] is True and found["wpid"] == "WP5636"
        assert found["name"] == "Mitophagy" and found["state"] == "on_main"
        missing = c.get("/api/pathways/9999").json()
        assert missing["exists"] is False and missing["wpid"] == "WP9999"
        assert missing["state"] == "absent"


def test_request_changes_endpoint(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]

        # A non-curator cannot request changes.
        assert c.post(f"/api/reviews/{pr}/request-changes", data={"note": "x"}).status_code == 403

        current["user"] = "curator"
        r = c.post(f"/api/reviews/{pr}/request-changes", data={"note": "Annotate the nodes."})
        assert r.status_code == 200
        assert r.json()["status"] == "changes_requested"
        # It leaves the open queue and shows under changes_requested.
        assert c.get("/api/reviews").json() == []
        cr = c.get("/api/reviews?status=changes_requested").json()
        assert [x["pr_number"] for x in cr] == [pr]
        # The note went out as a PR comment.
        comments = app.state._fake.issue_comments[(app.state.settings.content_repo, pr)]
        assert any("Annotate the nodes." in b for b in comments)


def test_pathway_info_detects_pending_new_submission(tmp_path):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    fake = FakeGitHubClient(default_branches={f"{repo}#{branch}": "base"})
    fake.open_pull_request(repo, head="submit/WP5642", base=branch, title="t", body="b")  # PR #1
    app, _current = _authed_app(tmp_path, fake=fake)
    with TestClient(app) as c:
        info = c.get("/api/pathways/5642").json()
        assert info["exists"] is False
        assert info["state"] == "pending_new"
        assert info["pr_number"] == 1


def test_revise_new_submission_end_to_end(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        sub = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()
        pr = sub["pr_number"]

        current["user"] = "curator"
        c.post(f"/api/reviews/{pr}/request-changes", data={"note": "annotate the nodes"})
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "changes_requested"

        # A stranger cannot revise someone else's submission.
        current["user"] = "mallory"
        forbidden = c.post(
            f"/api/reviews/{pr}/revise",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )
        assert forbidden.status_code == 403

        # The submitter revises → commits onto the SAME PR and re-opens the review.
        current["user"] = "bob"
        rev = c.post(
            f"/api/reviews/{pr}/revise",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
            data={"description": "added identifiers"},
        )
        assert rev.status_code == 201
        assert rev.json()["pr_number"] == pr  # no new PR
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "open"  # back in the queue


def test_revise_without_pending_submission_404(tmp_path):
    app, current = _authed_app(tmp_path)
    with TestClient(app) as c:
        current["user"] = "bob"
        r = c.post(
            "/api/reviews/9999/revise",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )
        assert r.status_code == 404


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

        # Even complete + curator, merge is blocked until the PR-preview CI is green (409).
        current["user"] = "curator"
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 409
        app.state._fake.previews[pr] = {"status": "ready"}

        # The curator approves → merges.
        ok = c.post(f"/api/reviews/{pr}/approve")
        assert ok.status_code == 200
        assert ok.json()["status"] == "merged"
        assert ok.json()["approved_by"] == "curator"
        assert pr in app.state._fake.merged
        assert c.get("/api/reviews").json() == []


def test_checklist_and_assign_require_curator(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        # A logged-in non-curator cannot mutate review state.
        assert (
            c.post(
                f"/api/reviews/{pr}/checklist", data={"key": "render_ok", "state": "pass"}
            ).status_code
            == 403
        )
        assert (
            c.post(f"/api/reviews/{pr}/assign", data={"curator": "curator"}).status_code == 403
        )
        # The curator can.
        current["user"] = "curator"
        assert (
            c.post(
                f"/api/reviews/{pr}/checklist", data={"key": "render_ok", "state": "pass"}
            ).status_code
            == 200
        )
        assert (
            c.post(f"/api/reviews/{pr}/assign", data={"curator": "curator"}).status_code == 200
        )


# -- GitHub App (bot) identity -------------------------------------------------------------


def test_submit_posts_mirror_comment(tmp_path):
    app, _current = _authed_app(tmp_path)
    with TestClient(app) as c:
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
    fake = app.state._fake
    repo = app.state.settings.content_repo
    assert (repo, pr) in fake.comments  # the bot mirrored the new submission
    assert "curation" in next(iter(fake.comments[(repo, pr)].values())).lower()


def test_approve_merges_via_bot_and_updates_mirror(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        current["user"] = "curator"
        detail = c.get(f"/api/reviews/{pr}").json()
        for item in detail["checklist"]:
            if item["required"]:
                c.post(f"/api/reviews/{pr}/checklist", data={"key": item["key"], "state": "pass"})
        app.state._fake.previews[pr] = {"status": "ready"}  # PR-preview CI green → merge allowed
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 200

    fake = app.state._fake
    repo = app.state.settings.content_repo
    assert pr in fake.merged  # merged through the bot identity
    # The mirror comment (single upserted comment) now reflects the merge.
    mirror = next(iter(fake.comments[(repo, pr)].values()))
    assert "**merged**." in mirror and "**Approved and merged by @curator.**" in mirror


def test_approve_503_without_bot_identity(tmp_path):
    # Configured app, logged-in curator, but no GitHub App → merge cannot run as the bot.
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}", curators=["curator"]
    )
    app = build_app(settings)
    fake = FakeGitHubClient(
        default_branches={f"{settings.content_repo}#{settings.default_branch}": "basesha"}
    )
    app.dependency_overrides[get_github_client] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: "curator"
    # Deliberately do NOT override the bot deps: state.bot_app is None → get_bot_client 503s.
    with TestClient(app) as c:
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        for item in c.get(f"/api/reviews/{pr}").json()["checklist"]:
            if item["required"]:
                c.post(f"/api/reviews/{pr}/checklist", data={"key": item["key"], "state": "pass"})
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 503
        assert pr not in fake.merged


# -- GitHub webhook: lock/reservation lifecycle on PR close (issue #8) ----------------------

import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402


def _signed(secret: str, payload: dict):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, sig


def _pr_closed_body(secret, pr_number, *, merged):
    return _signed(
        secret,
        {
            "action": "closed",
            "number": pr_number,
            "pull_request": {"number": pr_number, "merged": merged},
        },
    )


def test_webhook_merged_finalizes_review(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"], webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        body, sig = _pr_closed_body("whsec", pr, merged=True)
        r = c.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig},
        )
        assert r.status_code == 200 and r.json()["tracked"] is True
        # Review is now merged even though nobody clicked Approve in the app.
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "merged"


def test_webhook_closed_unmerged_releases_lock_and_reservation(tmp_path):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "basesha"},
        existing_files={f"{repo}#pathways/WP5636/WP5636.gpml": "oldsha"},
    )
    app, current = _authed_app(
        tmp_path, curators=["curator"], fake=fake, webhook_secret="whsec"
    )
    with TestClient(app) as c:
        current["user"] = "alice"
        pr = c.post(
            "/api/pathways/5636/update",
            files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
        ).json()["pr_number"]
        # Lock is held by alice on WP5636.
        assert app.state.locks.is_locked(5636)

        body, sig = _pr_closed_body("whsec", pr, merged=False)
        r = c.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig},
        )
        assert r.status_code == 200
        # PR closed outside the app → lock freed, review closed.
        assert not app.state.locks.is_locked(5636)
        assert c.get(f"/api/reviews/{pr}", ).json() is not None
        assert c.get("/api/reviews?status=closed").json()[0]["pr_number"] == pr


def test_webhook_rejects_bad_signature_and_missing_secret(tmp_path):
    # No secret configured → 503.
    app_no_secret, _ = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app_no_secret) as c:
        assert c.post("/webhooks/github", content=b"{}").status_code == 503

    app, _ = _authed_app(tmp_path, curators=["curator"], webhook_secret="whsec")
    with TestClient(app) as c:
        body, _sig = _pr_closed_body("whsec", 1, merged=True)
        r = c.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=deadbeef"},
        )
        assert r.status_code == 401


def test_webhook_is_idempotent(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"], webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        body, sig = _pr_closed_body("whsec", pr, merged=True)
        h = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig}
        assert c.post("/webhooks/github", content=body, headers=h).status_code == 200
        # A duplicate delivery is a harmless no-op (review already terminal).
        r2 = c.post("/webhooks/github", content=body, headers=h)
        assert r2.status_code == 200 and r2.json()["tracked"] is True
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "merged"


# -- pathway preview serving (issue #11) ----------------------------------------------------



def test_preview_route_serves_the_app_render(tmp_path):
    # Submitting renders both sides in-process, so the SVG is servable straight away — no CI
    # artifact, no second source (the artifact path was retired with the CI render).
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]  # assigns WP5637

        after = c.get(f"/previews/{pr}/after.svg")
        assert after.status_code == 200
        assert after.headers["content-type"].startswith("image/svg+xml")
        assert after.content.startswith(b"<svg")
        assert "sandbox" in after.headers.get("content-security-policy", "")

        # Unknown side → 404; unknown PR → 404.
        assert c.get(f"/previews/{pr}/sideways.svg").status_code == 404
        assert c.get("/previews/999999/after.svg").status_code == 404


def _login(client, login: str) -> None:
    """Set the signed session cookie the HTML pages read.

    The JSON API resolves identity through the ``get_current_user`` dependency (overridden in
    tests), but the server-rendered pages read ``request.session`` directly, so a page test has
    to carry a real session cookie. Mirrors what Starlette's SessionMiddleware writes.
    """
    import base64
    import json as _json

    from itsdangerous import TimestampSigner

    data = base64.b64encode(_json.dumps({"gh_login": login}).encode())
    signer = TimestampSigner("dev-insecure-change-me")  # the default session_secret in tests
    client.cookies.set("session", signer.sign(data).decode())


def test_dashboard_shows_the_render_after_changes_are_requested(tmp_path):
    # The render used to be computed for open reviews only, so every other filter showed the
    # "no render" state while the SVG sat in the cache.
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        current["user"] = "curator"
        c.post(f"/api/reviews/{pr}/request-changes", data={"note": "add an identifier"})

        _login(c, "curator")
        page = c.get("/dashboard", params={"status": "changes_requested"})
        assert page.status_code == 200
        assert f"/previews/{pr}/after.svg".encode() in page.content
        assert b"No render on file" not in page.content


def test_preview_missing_side_serves_placeholder(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        # A new pathway has no "before" — the frame stays intact instead of breaking.
        r = c.get(f"/previews/{pr}/before.svg")
        assert r.status_code == 200 and b"Preview unavailable" in r.content


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


def _notice_client(tmp_path, notice):
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}", site_notice=notice
    )
    return TestClient(build_app(settings))


def test_site_notice_shows_on_every_page_when_configured(tmp_path):
    # A deployment can be pointed at a target that cannot publish, and nothing on screen said so.
    # It has to appear on the submit page above all, since that is where the promise is made.
    notice = "Sandbox deployment: submissions here are not published."
    with _notice_client(tmp_path, notice) as c:
        for path in ("/", "/dashboard"):
            body = c.get(path).text
            assert notice in body
            assert 'class="site-notice"' in body


def test_no_site_notice_element_when_unset(tmp_path):
    # Empty must mean no banner at all, not an empty amber bar on every page.
    with _notice_client(tmp_path, "") as c:
        assert "site-notice" not in c.get("/").text


def test_blank_site_notice_is_treated_as_unset(tmp_path):
    with _notice_client(tmp_path, "   ") as c:
        assert "site-notice" not in c.get("/").text


def test_site_notice_is_escaped(tmp_path):
    # It comes from deploy config rather than a user, but config is not markup and this renders
    # on every page including logged-out ones.
    with _notice_client(tmp_path, "<script>alert(1)</script>") as c:
        body = c.get("/").text
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body
