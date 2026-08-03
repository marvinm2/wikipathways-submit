"""How often one account may open a pull request on the content repo (issue #21).

The unit is the review row, because that is what the limiter counts: it is created in the same
request that opens the pull request, so a row is a pull request with nothing to keep in step.
"""
from __future__ import annotations

import io
from datetime import timedelta

import pytest

from app.models import Review, ReviewStatus, utcnow
from app.ratelimit import RateLimited, SubmissionRateLimiter

from .test_api import GOOD_GPML, _authed_app

pytest_plugins = ()


def _limiter(session_factory, *, limit=3, minutes=60) -> SubmissionRateLimiter:
    return SubmissionRateLimiter(
        session_factory, limit=limit, window=timedelta(minutes=minutes)
    )


def _submissions(session_factory, submitter: str, count: int, *, minutes_ago: float = 0) -> None:
    when = utcnow() - timedelta(minutes=minutes_ago)
    with session_factory() as s:
        for i in range(count):
            s.add(
                Review(
                    pr_number=s.query(Review).count() + i + 1,
                    wpid=5636 + i,
                    submitter=submitter,
                    kind="new",
                    status=ReviewStatus.OPEN,
                    checklist=[],
                    created_at=when,
                    updated_at=when,
                )
            )
        s.commit()


def test_under_the_limit_passes(session_factory):
    _submissions(session_factory, "bob", 2)
    _limiter(session_factory).check("bob")  # no raise


def test_at_the_limit_refuses(session_factory):
    _submissions(session_factory, "bob", 3)
    with pytest.raises(RateLimited) as exc:
        _limiter(session_factory).check("bob")
    assert exc.value.retry_after > 0


def test_the_window_moves(session_factory):
    # Three submissions, but yesterday's. The limit is a rate, not a quota.
    _submissions(session_factory, "bob", 3, minutes_ago=60 * 24)
    _limiter(session_factory).check("bob")


def test_one_account_does_not_spend_anothers_budget(session_factory):
    _submissions(session_factory, "bob", 5)
    _limiter(session_factory).check("carol")


def test_retry_after_is_when_the_window_actually_frees(session_factory):
    """Not the whole window: the oldest submission inside it is 50 minutes old, so a 60 minute
    window has 10 minutes left to run, and telling the client to wait an hour is wrong."""
    _submissions(session_factory, "bob", 3, minutes_ago=50)
    with pytest.raises(RateLimited) as exc:
        _limiter(session_factory).check("bob")
    assert 8 * 60 <= exc.value.retry_after <= 11 * 60


def test_a_limit_of_zero_disables_it(session_factory):
    _submissions(session_factory, "bob", 50)
    limiter = _limiter(session_factory, limit=0)
    assert limiter.enabled is False
    limiter.check("bob")


def test_the_refusal_says_what_to_do_about_it(session_factory):
    _submissions(session_factory, "bob", 3)
    with pytest.raises(RateLimited) as exc:
        _limiter(session_factory).check("bob")
    message = str(exc.value)
    # A submitter reading this needs to know nothing was lost and roughly how long to wait.
    assert "Nothing is lost" in message
    assert "60 minutes" in message


# -- through the endpoint ---------------------------------------------------------------------


def _upload(client):
    return client.post(
        "/api/submit", files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")}
    )


def test_submit_refuses_with_429_and_retry_after(tmp_path):
    from fastapi.testclient import TestClient

    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        app.state.rate_limiter = SubmissionRateLimiter(
            app.state.session_factory, limit=2, window=timedelta(minutes=60)
        )
        current["user"] = "bob"
        assert _upload(c).status_code == 201
        assert _upload(c).status_code == 201

        refused = _upload(c)

    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
    # And it cost the content repo nothing: two pull requests were opened, not three.
    assert len(app.state._fake.pulls) == 2


def test_the_limit_is_per_account_not_per_process(tmp_path):
    from fastapi.testclient import TestClient

    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        app.state.rate_limiter = SubmissionRateLimiter(
            app.state.session_factory, limit=1, window=timedelta(minutes=60)
        )
        current["user"] = "bob"
        assert _upload(c).status_code == 201
        assert _upload(c).status_code == 429
        # Carol's first submission is her first, whatever bob has been doing.
        current["user"] = "carol"
        assert _upload(c).status_code == 201


def test_answering_a_change_request_is_not_rate_limited(tmp_path):
    """Re-uploading onto a pull request that already exists opens nothing and notifies nobody
    new. Refusing it would punish the ordinary way a submitter responds to a curator."""
    from fastapi.testclient import TestClient

    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _upload(c).json()["pr_number"]
        app.state.rate_limiter = SubmissionRateLimiter(
            app.state.session_factory, limit=1, window=timedelta(minutes=60)
        )
        # Over the limit for anything new...
        assert _upload(c).status_code == 429
        # ...and still able to revise what is already open.
        revised = c.post(
            f"/api/reviews/{pr}/revise",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )

    assert revised.status_code == 201
