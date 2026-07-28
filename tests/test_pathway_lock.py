from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from app.locks import LockUnavailable, PathwayLockRegistry


def test_acquire_and_release(session_factory):
    reg = PathwayLockRegistry(session_factory)
    lock = reg.acquire(5636, "alice", pr_number=12)
    assert lock.held_by == "alice"
    assert reg.is_locked(5636)
    assert reg.release(5636, "alice") is True
    assert not reg.is_locked(5636)


def test_second_holder_is_refused(session_factory):
    reg = PathwayLockRegistry(session_factory)
    reg.acquire(5636, "alice")
    with pytest.raises(LockUnavailable) as ei:
        reg.acquire(5636, "bob")
    assert ei.value.held_by == "alice"


def test_reacquire_by_same_holder_refreshes(session_factory):
    reg = PathwayLockRegistry(session_factory, ttl=timedelta(days=1))
    first = reg.acquire(5636, "alice")
    first_expiry = first.expires_at
    again = reg.acquire(5636, "alice", pr_number=7)
    assert again.pr_number == 7
    assert again.expires_at >= first_expiry


def test_open_pr_scanner_blocks_acquisition(session_factory):
    # A raw PR opened outside the app must block check-out even with no lock row present.
    reg = PathwayLockRegistry(session_factory, open_pr_scanner=lambda wpid: wpid == 5636)
    with pytest.raises(LockUnavailable) as ei:
        reg.acquire(5636, "alice")
    assert "open GitHub PR" in ei.value.reason
    # A different pathway with no open PR is fine.
    assert reg.acquire(5637, "alice").held_by == "alice"


def test_expired_lock_is_reclaimed(session_factory):
    reg = PathwayLockRegistry(session_factory, ttl=timedelta(seconds=-1))
    reg.acquire(5636, "alice")  # already expired on write
    # A different user can now take it because expire_stale() runs first.
    assert reg.acquire(5636, "bob").held_by == "bob"


def test_release_someone_elses_lock_refused_unless_forced(session_factory):
    reg = PathwayLockRegistry(session_factory)
    reg.acquire(5636, "alice")
    with pytest.raises(LockUnavailable):
        reg.release(5636, "bob")
    # Curator force-release works regardless of holder.
    assert reg.release(5636, "curator", force=True) is True
    assert not reg.is_locked(5636)


def test_release_when_unlocked_returns_false(session_factory):
    reg = PathwayLockRegistry(session_factory)
    assert reg.release(9999, "alice") is False


def test_concurrent_acquire_single_winner(session_factory):
    """N users racing to check out the SAME pathway: exactly one wins, rest get LockUnavailable."""
    reg = PathwayLockRegistry(session_factory)
    n_threads = 40
    winners: list[str] = []
    refused = 0
    lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker(i: int) -> None:
        nonlocal refused
        barrier.wait()
        try:
            reg.acquire(5636, f"user{i}")
            with lock:
                winners.append(f"user{i}")
        except LockUnavailable:
            with lock:
                refused += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert refused == n_threads - 1


def test_the_app_refuses_an_update_when_a_raw_pr_already_touches_the_pathway(tmp_path):
    """The lock's table only knows about edits that came through this app. On the deployed
    target most pull requests do not: someone opening one by hand is exactly the second writer
    the lock exists to notice."""
    import io

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.github import FakeGitHubClient
    from app.main import build_app, get_bot_optional, get_current_user, get_github_client
    from tests.test_api import GOOD_GPML

    repo = "wikipathways/wikipathways-database"
    fake = FakeGitHubClient(
        default_branches={f"{repo}#main": "basesha"},
        existing_files={f"{repo}#pathways/WP554/WP554.gpml": "oldsha"},
    )
    # Somebody's raw pull request, opened outside the portal, editing the same pathway.
    raw = fake.open_pull_request(repo, head="egonw-patch", base="main", title="fix", body="")
    fake.put_file(repo, "egonw-patch", "pathways/WP554/WP554.gpml", "<Pathway/>", "by hand")
    assert fake.find_open_pr_touching(repo, "pathways/WP554/") == raw.number

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        content_repo=repo,
        preview_cache_dir=str(tmp_path / "preview-cache"),
    )
    app = build_app(settings)
    app.dependency_overrides[get_github_client] = lambda: fake
    app.dependency_overrides[get_bot_optional] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: "alice"
    with TestClient(app) as client:
        # The scan runs outside any request, so it reads the bot through app.state.
        app.state.bot_client_provider = lambda: fake
        resp = client.post(
            "/api/pathways/554/update",
            files={"file": ("wp554.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )

    assert resp.status_code == 409
    assert "open GitHub PR" in resp.json()["detail"]["reason"]


def test_the_apps_own_update_pr_does_not_trip_its_own_scanner(tmp_path):
    """The update flow acquires the lock a second time to record the pull request it just
    opened. A scan on that refresh finds that very pull request and refuses the check-out its
    own holder is completing — which would 409 every update on a deployment where the bot is
    configured, leaving the lock held and no review row behind."""
    import io

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.github import FakeGitHubClient
    from app.main import build_app, get_bot_optional, get_current_user, get_github_client
    from app.models import Review
    from tests.test_api import GOOD_GPML

    repo = "wikipathways/wikipathways-database"
    fake = FakeGitHubClient(
        default_branches={f"{repo}#main": "basesha"},
        existing_files={f"{repo}#pathways/WP554/WP554.gpml": "oldsha"},
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        content_repo=repo,
        preview_cache_dir=str(tmp_path / "preview-cache"),
    )
    app = build_app(settings)
    app.dependency_overrides[get_github_client] = lambda: fake
    app.dependency_overrides[get_bot_optional] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: "alice"
    with TestClient(app) as client:
        app.state.bot_client_provider = lambda: fake  # as a configured deployment has

        resp = client.post(
            "/api/pathways/554/update",
            files={"file": ("wp554.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )

        assert resp.status_code == 201, resp.text
        pr = resp.json()["pr_number"]
        # The review row is what makes it visible in the dashboard at all.
        with app.state.session_factory() as s:
            assert s.get(Review, pr) is not None
        assert app.state.locks.get(554).pr_number == pr
