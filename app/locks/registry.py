"""Pathway check-out lock — one open edit per pathway at a time (design §4.3).

Acquiring a lock is what lets a submitter start an *update*; it structurally prevents the
#90 "two divergent GPMLs, unmergeable" failure rather than resolving it after the fact.

Two things make this more than a boolean flag:

1. **The lock cannot assume it is the only writer.** A power user can still open a raw GitHub
   PR directly, bypassing the app. So acquisition also runs an injected ``open_pr_scanner`` and
   refuses if an open PR already touches the pathway.
2. **Locks must not become permanent blocks.** They auto-expire (TTL) and a curator can
   force-release, so an abandoned check-out never silently freezes a pathway forever.

Atomicity mirrors the WPID allocator: the pathway id is the primary key, so two simultaneous
acquisitions of the same pathway resolve to exactly one winner via the uniqueness constraint.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import PathwayLock, utcnow

#: Callable ``(wpid) -> bool``: True if an open GitHub PR already touches this pathway.
OpenPrScanner = Callable[[int], bool]


class LockUnavailable(RuntimeError):
    """Raised when a pathway cannot be checked out."""

    def __init__(self, reason: str, *, held_by: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.held_by = held_by


class PathwayLockRegistry:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        ttl: timedelta = timedelta(days=3),
        open_pr_scanner: OpenPrScanner | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._ttl = ttl
        self._open_pr_scanner = open_pr_scanner

    def expire_stale(self) -> int:
        """Delete locks past their TTL. Returns the number released."""
        now = utcnow()
        with self._session_factory() as s:
            result = s.execute(
                delete(PathwayLock).where(
                    PathwayLock.expires_at.is_not(None),
                    PathwayLock.expires_at < now,
                )
            )
            s.commit()
            return result.rowcount or 0

    def get(self, wpid: int) -> PathwayLock | None:
        with self._session_factory() as s:
            return s.get(PathwayLock, wpid)

    def is_locked(self, wpid: int) -> bool:
        self.expire_stale()
        return self.get(wpid) is not None

    def acquire(
        self, wpid: int, held_by: str, *, pr_number: int | None = None
    ) -> PathwayLock:
        """Check out a pathway for editing.

        Refuses if the pathway is already checked out by someone else, or if an open GitHub PR
        already touches it. Re-acquiring one you already hold refreshes the TTL.
        """
        self.expire_stale()

        # A raw PR opened outside the app is an external writer the lock can't see in its table.
        if self._open_pr_scanner is not None and self._open_pr_scanner(wpid):
            raise LockUnavailable(
                "an open GitHub PR already touches this pathway (opened outside the app)"
            )

        now = utcnow()
        with self._session_factory() as s:
            existing = s.get(PathwayLock, wpid)
            if existing is not None:
                if existing.held_by == held_by:
                    existing.expires_at = now + self._ttl
                    if pr_number is not None:
                        existing.pr_number = pr_number
                    s.commit()
                    return existing
                raise LockUnavailable(
                    f"pathway WP{wpid} is checked out by {existing.held_by}",
                    held_by=existing.held_by,
                )

            lock = PathwayLock(
                wpid=wpid,
                held_by=held_by,
                acquired_at=now,
                expires_at=now + self._ttl,
                pr_number=pr_number,
            )
            s.add(lock)
            try:
                s.commit()
            except IntegrityError as exc:
                # Lost the race to a concurrent acquirer.
                raise LockUnavailable(
                    f"pathway WP{wpid} was checked out concurrently"
                ) from exc
            return lock

    def release(self, wpid: int, held_by: str, *, force: bool = False) -> bool:
        """Release a lock. ``force=True`` is the curator override (any holder).

        Returns True if a lock was released, False if none was held. Raises LockUnavailable if
        a non-force caller tries to release someone else's lock.
        """
        with self._session_factory() as s:
            lock = s.get(PathwayLock, wpid)
            if lock is None:
                return False
            if not force and lock.held_by != held_by:
                raise LockUnavailable(
                    f"pathway WP{wpid} is held by {lock.held_by}, not {held_by}",
                    held_by=lock.held_by,
                )
            s.delete(lock)
            s.commit()
            return True
