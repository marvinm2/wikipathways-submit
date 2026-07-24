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
