from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from app.models import ReservationStatus, WpidReservation
from app.wpid import WpidAllocationError, WpidAllocator, format_wpid, parse_wpid


def test_parse_and_format_roundtrip():
    assert format_wpid(5637) == "WP5637"
    assert parse_wpid("WP5637") == 5637
    assert parse_wpid("  WP42 ") == 42
    with pytest.raises(ValueError):
        parse_wpid("5637")
    with pytest.raises(ValueError):
        parse_wpid("WPX")


def test_allocate_from_floor_only(session_factory):
    # Empty local table; floor comes from the (mocked) tree ∪ open PRs.
    alloc = WpidAllocator(session_factory, floor_provider=lambda: 5636)
    assert alloc.allocate("alice") == 5637
    assert alloc.allocate("bob") == 5638


def test_allocate_unions_floor_and_reservations(session_factory):
    # Floor drops (e.g. tree read lags) but existing reservations must still raise the max —
    # this is the WP5637-5641 union bug guard.
    calls = iter([5640, 0, 0])
    alloc = WpidAllocator(session_factory, floor_provider=lambda: next(calls))
    assert alloc.allocate("alice") == 5641  # from floor 5640
    assert alloc.allocate("bob") == 5642  # floor now 0, but reservation 5641 floors it
    assert alloc.allocate("carol") == 5643


def test_expiry_reclaims_id(session_factory):
    from datetime import timedelta

    alloc = WpidAllocator(session_factory, floor_provider=lambda: 0, ttl=timedelta(seconds=-1))
    # ttl in the past → the reservation is already expired the instant it is written.
    first = alloc.allocate("alice")
    assert first == 1
    # Next allocate() runs expire_stale() first, reclaiming id 1.
    second = alloc.allocate("bob")
    assert second == 1


def test_merged_reservation_keeps_max_monotonic(session_factory):
    alloc = WpidAllocator(session_factory, floor_provider=lambda: 0)
    wpid = alloc.allocate("alice")
    alloc.mark_merged(wpid, pr_number=99)
    with session_factory() as s:
        row = s.get(WpidReservation, wpid)
        assert row.status == ReservationStatus.MERGED
        assert row.expires_at is None
        assert row.pr_number == 99
    # A merged reservation is never expired and still floors the next id.
    assert alloc.expire_stale() == 0
    assert alloc.allocate("bob") == wpid + 1


def test_release_returns_id_to_pool(session_factory):
    alloc = WpidAllocator(session_factory, floor_provider=lambda: 0)
    wpid = alloc.allocate("alice")
    assert alloc.release(wpid) is True
    assert alloc.allocate("bob") == wpid  # id was freed
    # A merged reservation cannot be released.
    alloc.mark_merged(wpid)
    assert alloc.release(wpid) is False


def test_retry_budget_exhausted_raises(session_factory):
    alloc = WpidAllocator(session_factory, floor_provider=lambda: 0, max_retries=1)
    # Pre-occupy id 1 so the single attempt collides and there is no retry left.
    with session_factory() as s:
        s.add(WpidReservation(wpid=1, reserved_by="squatter"))
        s.commit()
    # Monkeypatch _local_max to always report 0 so the candidate stays 1 and always collides.
    alloc._local_max = lambda _s: 0  # type: ignore[method-assign]
    with pytest.raises(WpidAllocationError):
        alloc.allocate("alice")


def test_concurrent_allocation_no_collisions(session_factory):
    """The money test: N threads allocating at once yield N distinct, contiguous ids.

    This is the direct guard against the real-world WP5637-5641 collision. If the primary-key
    atomicity were wrong, two threads would return the same WPID and the set would shrink.
    """
    floor = 5636
    n_threads = 50
    alloc = WpidAllocator(session_factory, floor_provider=lambda: floor)

    results: list[int] = []
    errors: list[Exception] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker() -> None:
        barrier.wait()  # maximize contention: everyone computes the same first candidate
        try:
            wpid = alloc.allocate("racer")
            with lock:
                results.append(wpid)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == n_threads
    assert len(set(results)) == n_threads, "duplicate WPID allocated — collision!"
    assert sorted(results) == list(range(floor + 1, floor + 1 + n_threads))


def test_a_reclaimed_reservation_says_how_long_it_was_held(session_factory, caplog):
    """Issue #23, the reservation half. An expiry means an identifier was held for the whole
    window without landing, which is the evidence the TTL would ever be corrected from."""
    import logging

    alloc = WpidAllocator(session_factory, lambda: 0, ttl=timedelta(seconds=-1))
    wpid = alloc.allocate("alice", pr_number=42)

    with caplog.at_level(logging.INFO, logger="wpsubmit.wpid"):
        assert alloc.expire_stale() == 1

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert f"WP{wpid}" in message
    assert "alice" in message
    assert "42" in message


def test_a_quiet_reclaim_pass_logs_nothing(session_factory, caplog):
    import logging

    alloc = WpidAllocator(session_factory, lambda: 0)
    alloc.allocate("alice")
    with caplog.at_level(logging.INFO, logger="wpsubmit.wpid"):
        assert alloc.expire_stale() == 0
    assert caplog.records == []
