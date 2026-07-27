"""SQLAlchemy models for the transactional registry.

These two tables carry the invariants today's raw-PR workflow violates (design §4.2/§4.3):

- ``WpidReservation`` — atomic WPID allocation. The WPID is the primary key, so the database
  itself guarantees no two reservations share an ID (the bug behind WP5637-5641). Reservations
  that never become a merged PR expire and the ID returns to the pool.
- ``PathwayLock`` — one open edit per pathway at a time, structurally preventing the
  unmergeable-concurrent-edit failure (#90).
"""
from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Enum, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.review.checklist import default_checklist


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ReservationStatus(enum.StrEnum):
    #: Allocated, PR not yet merged. Counts toward the max; expires if abandoned.
    RESERVED = "reserved"
    #: PR merged — a real, permanent WPID. Kept so the max stays monotonic even before the
    #: repo tree read catches up.
    MERGED = "merged"


class WpidReservation(Base):
    __tablename__ = "wpid_reservation"

    # No autoincrement: the allocator computes the value over tree ∪ open PRs ∪ reservations
    # and the PK's uniqueness is what makes concurrent allocation collision-free.
    wpid: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    reserved_by: Mapped[str] = mapped_column(String(255))
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, native_enum=False, length=16),
        default=ReservationStatus.RESERVED,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<WpidReservation WP{self.wpid} {self.status.value} by {self.reserved_by}>"


class PathwayLock(Base):
    __tablename__ = "pathway_lock"

    # One row per checked-out pathway. Presence of the row == the pathway is locked.
    wpid: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    held_by: Mapped[str] = mapped_column(String(255))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<PathwayLock WP{self.wpid} held by {self.held_by}>"


class ReviewStatus(enum.StrEnum):
    OPEN = "open"
    CHANGES_REQUESTED = "changes_requested"
    MERGED = "merged"
    CLOSED = "closed"


class Review(Base):
    """Curation/approval state for one submission PR — the app-owned source of truth (§4.5).

    Keyed by PR number. The checklist is stored as JSON (list of item dicts) so the template can
    change without a migration.
    """

    __tablename__ = "review"

    pr_number: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    wpid: Mapped[int] = mapped_column(Integer)
    submitter: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))  # "new" | "update"
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, native_enum=False, length=24),
        default=ReviewStatus.OPEN,
    )
    assigned_curator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checklist: Mapped[list] = mapped_column(JSON, default=default_checklist)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optimistic-concurrency counter: the ORM stamps every UPDATE with WHERE version=<read> and
    # bumps it, so a lost update (issue #15) surfaces as StaleDataError instead of silently
    # overwriting. See CurationService.set_checklist_item, which retries on that conflict.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __mapper_args__ = {"version_id_col": version}

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Review PR#{self.pr_number} WP{self.wpid} {self.status.value}>"
