"""Atomic WPID allocator.

The bug this replaces (design §1, appendix): "next id = highest in the merged tree + 1" read
only the merged tree, so WP5637-5641 got reserved four times over across in-flight PRs.

The fix has two parts:

1. **Union floor.** The next id is ``1 + max(WPID)`` over **repo tree ∪ open PRs ∪ live
   reservations**. The tree+PRs part is supplied by an injected ``floor_provider`` (a callable
   returning the current max WPID number seen on GitHub); the reservations part is the max of
   the local table. Reading only the tree is exactly the historical bug.

2. **Atomicity via the primary key.** Two simultaneous new-pathway submissions must not receive
   the same id. Rather than lock-and-compute (dialect-specific, and SQLite has no
   ``SELECT ... FOR UPDATE``), we compute a candidate and ``INSERT`` it; the WPID is the table's
   primary key, so a colliding second insert fails with ``IntegrityError`` and we recompute and
   retry. The database's uniqueness constraint is the source of truth — correct on both SQLite
   and PostgreSQL.

``floor_provider`` is intentionally a plain callable so the allocator is testable without a
network and the GitHub read strategy can evolve independently (see ``github_floor.py``).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.models import ReservationStatus, WpidReservation, utcnow

_WPID_RE = re.compile(r"^WP(\d+)$")

#: Callable returning the current max WPID *number* over the repo tree ∪ open PRs.
#: Return 0 if the source is empty/unavailable — the local reservations still floor the result.
FloorProvider = Callable[[], int]


class WpidAllocationError(RuntimeError):
    """Raised when a fresh WPID could not be allocated within the retry budget."""


def format_wpid(number: int) -> str:
    return f"WP{number}"


def parse_wpid(wpid: str) -> int:
    m = _WPID_RE.match(wpid.strip())
    if not m:
        raise ValueError(f"not a WPID: {wpid!r}")
    return int(m.group(1))


class WpidAllocator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        floor_provider: FloorProvider,
        *,
        ttl: timedelta = timedelta(days=14),
        max_retries: int = 50,
    ) -> None:
        self._session_factory = session_factory
        self._floor_provider = floor_provider
        self._ttl = ttl
        self._max_retries = max_retries

    def expire_stale(self) -> int:
        """Delete reserved-but-abandoned rows past their TTL, returning IDs to the pool.

        Merged reservations are never expired. Deletion (rather than a status flip) frees the
        primary key so the id can be re-allocated cleanly. Returns the number reclaimed.
        """
        now = utcnow()
        with self._session_factory() as s:
            result = s.execute(
                delete(WpidReservation).where(
                    WpidReservation.status == ReservationStatus.RESERVED,
                    WpidReservation.expires_at.is_not(None),
                    WpidReservation.expires_at < now,
                )
            )
            s.commit()
            return result.rowcount or 0

    def _local_max(self, session: Session) -> int:
        return session.execute(select(func.max(WpidReservation.wpid))).scalar() or 0

    def allocate(self, reserved_by: str, *, pr_number: int | None = None) -> int:
        """Reserve and return the next free WPID number, atomically.

        Concurrency guarantee: under any number of concurrent callers, every returned value is
        distinct — enforced by the ``wpid`` primary key, not by application-level locking.
        """
        self.expire_stale()
        floor = max(0, int(self._floor_provider()))

        last_error: Exception | None = None
        for _attempt in range(self._max_retries):
            try:
                with self._session_factory() as s:
                    candidate = max(floor, self._local_max(s)) + 1
                    s.add(
                        WpidReservation(
                            wpid=candidate,
                            reserved_by=reserved_by,
                            reserved_at=utcnow(),
                            expires_at=utcnow() + self._ttl,
                            pr_number=pr_number,
                            status=ReservationStatus.RESERVED,
                        )
                    )
                    s.commit()
                    return candidate
            except IntegrityError as exc:
                # Someone else committed this candidate first — recompute and retry.
                last_error = exc
                continue
            except OperationalError as exc:
                # SQLite "database is locked" beyond busy_timeout — retry.
                last_error = exc
                continue

        raise WpidAllocationError(
            f"could not allocate a WPID after {self._max_retries} attempts"
        ) from last_error

    def mark_merged(self, wpid: int, *, pr_number: int | None = None) -> None:
        """Promote a reservation to MERGED (permanent). Idempotent-ish: inserts if absent."""
        with self._session_factory() as s:
            row = s.get(WpidReservation, wpid)
            if row is None:
                row = WpidReservation(wpid=wpid, reserved_by="", reserved_at=utcnow())
                s.add(row)
            row.status = ReservationStatus.MERGED
            row.expires_at = None
            if pr_number is not None:
                row.pr_number = pr_number
            s.commit()

    def attach_pr(self, wpid: int, pr_number: int) -> None:
        """Record the PR number on a reservation once its PR has been opened."""
        with self._session_factory() as s:
            row = s.get(WpidReservation, wpid)
            if row is not None:
                row.pr_number = pr_number
                s.commit()

    def release(self, wpid: int) -> bool:
        """Drop a reservation (e.g. PR closed unmerged), returning the id to the pool."""
        with self._session_factory() as s:
            row = s.get(WpidReservation, wpid)
            if row is None or row.status == ReservationStatus.MERGED:
                return False
            s.delete(row)
            s.commit()
            return True
