"""Curation service (design §4.5): the reviewer's home + approve-that-merges.

Approval state is owned by the app (the ``review`` table) so the dashboard and the read-only PR
comment mirror never diverge. ``approve_and_merge`` is the one privileged action: it is gated to
the curator whitelist, requires the structured checklist to be complete, merges the PR, and then
completes the lifecycle — promoting the WPID reservation to MERGED and releasing the pathway lock.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.github import GitHubClient
from app.locks import PathwayLockRegistry
from app.models import Review, ReviewStatus, utcnow
from app.review.checklist import ChecklistState, is_complete, is_valid_key
from app.wpid import WpidAllocator


class ReviewNotFound(RuntimeError):
    pass


class NotACurator(RuntimeError):
    """The acting user is not on the curator whitelist."""


class ChecklistIncomplete(RuntimeError):
    """Approval attempted before every required checklist item is marked pass."""


class CurationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        github: GitHubClient | None,
        *,
        repo: str,
        curators: Iterable[str],
        allocator: WpidAllocator | None = None,
        locks: PathwayLockRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._github = github
        self._repo = repo
        self._curators = set(curators)
        self._allocator = allocator
        self._locks = locks

    def is_curator(self, user: str) -> bool:
        return user in self._curators

    def register(self, *, pr_number: int, wpid: int, submitter: str, kind: str) -> Review:
        """Create the review row for a freshly opened submission PR (idempotent by PR number)."""
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                review = Review(
                    pr_number=pr_number, wpid=wpid, submitter=submitter, kind=kind
                )
                s.add(review)
                s.commit()
            return review

    def list_queue(self, *, status: ReviewStatus = ReviewStatus.OPEN) -> list[Review]:
        with self._session_factory() as s:
            return list(
                s.execute(
                    select(Review).where(Review.status == status).order_by(Review.created_at)
                ).scalars()
            )

    def get(self, pr_number: int) -> Review:
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                raise ReviewNotFound(f"no review for PR #{pr_number}")
            return review

    def assign(self, pr_number: int, curator: str) -> Review:
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                raise ReviewNotFound(f"no review for PR #{pr_number}")
            review.assigned_curator = curator
            s.commit()
            return review

    def set_checklist_item(
        self, pr_number: int, key: str, state: str, note: str = ""
    ) -> Review:
        if not is_valid_key(key):
            raise ValueError(f"unknown checklist item: {key}")
        if state not in {s.value for s in ChecklistState}:
            raise ValueError(f"invalid checklist state: {state}")
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                raise ReviewNotFound(f"no review for PR #{pr_number}")
            # Rebuild the list so the JSON column is marked dirty.
            checklist = [dict(item) for item in review.checklist]
            for item in checklist:
                if item["key"] == key:
                    item["state"] = state
                    item["note"] = note
            review.checklist = checklist
            s.commit()
            return review

    def approve_and_merge(self, pr_number: int, curator: str) -> Review:
        if curator not in self._curators:
            raise NotACurator(f"{curator} is not on the curator whitelist")
        if self._github is None:
            raise RuntimeError("no GitHub client configured for merge")

        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                raise ReviewNotFound(f"no review for PR #{pr_number}")
            if not is_complete(review.checklist):
                raise ChecklistIncomplete(
                    f"PR #{pr_number}: required checklist items are not all passed"
                )
            wpid = review.wpid

        # Merge first; only mutate our state if GitHub accepts the merge.
        self._github.merge_pull_request(self._repo, pr_number)

        # Complete the lifecycle: WPID becomes permanent, pathway lock frees.
        if self._allocator is not None:
            self._allocator.mark_merged(wpid, pr_number=pr_number)
        if self._locks is not None:
            self._locks.release(wpid, curator, force=True)

        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            review.status = ReviewStatus.MERGED
            review.approved_by = curator
            review.merged_at = utcnow()
            s.commit()
            return review
