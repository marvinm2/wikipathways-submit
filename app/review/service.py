"""Curation service (design §4.5): the reviewer's home + approve-that-merges.

Approval state is owned by the app (the ``review`` table) so the dashboard and the read-only PR
comment mirror never diverge. ``approve_and_merge`` is the one privileged action: it is gated to
the curator whitelist, requires the structured checklist to be complete, merges the PR, and then
completes the lifecycle — promoting the WPID reservation to MERGED and releasing the pathway lock.
"""
from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.curators import CuratorRegistry
from app.github import GitHubClient, GitHubError
from app.locks import PathwayLockRegistry
from app.models import Review, ReviewStatus, utcnow
from app.review.checklist import ChecklistState, is_complete, is_valid_key
from app.wpid import WpidAllocator

#: Hidden token embedded in the mirror comment so we update the same one instead of spamming.
MIRROR_MARKER = "<!-- wikipathways-submit:mirror -->"


def render_mirror_comment(review: Review, repo: str) -> str:
    """Render the read-only PR mirror comment (design §4.5): checklist + approval state.

    Approval always flows through the app, so this comment is a *mirror* — it tells GitHub-native
    reviewers the current state and points them back to the dashboard to act.
    """
    lines = [
        MIRROR_MARKER,
        f"### WikiPathways curation — WP{review.wpid}",
        "",
        f"**Status:** `{review.status.value}` · **Submitter:** @{review.submitter} · "
        f"**Kind:** {review.kind}"
        + (f" · **Assigned:** @{review.assigned_curator}" if review.assigned_curator else ""),
        "",
        "| Check | State |",
        "|---|---|",
    ]
    for item in review.checklist:
        req = " *(required)*" if item.get("required") else ""
        note = f" — {item['note']}" if item.get("note") else ""
        lines.append(f"| {item['label']}{req}{note} | `{item.get('state', 'pending')}` |")
    if review.approved_by:
        lines += ["", f"**Approved & merged by** @{review.approved_by}."]
    lines += [
        "",
        "> This comment is **read-only** and auto-generated. Review and approve in the "
        "curation dashboard — approval flows through the app so this comment and the dashboard "
        "cannot disagree.",
    ]
    return "\n".join(lines)


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
        curators: CuratorRegistry,
        allocator: WpidAllocator | None = None,
        locks: PathwayLockRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._github = github
        self._repo = repo
        self._curators = curators
        self._allocator = allocator
        self._locks = locks

    def is_curator(self, user: str) -> bool:
        return self._curators.is_curator(user)

    def _maybe_mirror(self, review: Review) -> None:
        """Best-effort: sync the read-only PR mirror comment via the bot client.

        Never fail the primary action on a comment hiccup — the app dashboard is the source of
        truth; the mirror is a convenience for GitHub-native reviewers.
        """
        if self._github is None:
            return
        try:
            self._github.upsert_issue_comment(
                self._repo,
                review.pr_number,
                render_mirror_comment(review, self._repo),
                marker=MIRROR_MARKER,
            )
        except (GitHubError, httpx.HTTPError):
            # Both API status errors (GitHubError) AND transport failures (httpx: connect
            # refused, timeout, DNS) — raised by the real client *before* _raise_for runs — must
            # be swallowed. The merge/PR has already happened; a comment hiccup must not surface
            # as a 500 on an action that already succeeded.
            pass

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
            self._maybe_mirror(review)
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
            self._maybe_mirror(review)
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
            self._maybe_mirror(review)
            return review

    def approve_and_merge(self, pr_number: int, curator: str) -> Review:
        if not self._curators.is_curator(curator):
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
            self._maybe_mirror(review)
            return review

    def handle_pr_closed(self, pr_number: int, *, merged: bool) -> Review | None:
        """React to a PR closed/merged **outside** the app (webhook, issue #8).

        Frees the pathway lock, finalises the WPID reservation (permanent if merged, returned to
        the pool if closed unmerged), and moves the review to a terminal state. Idempotent: a
        duplicate delivery — or the webhook for a merge the app itself performed — is a no-op
        because the review is already terminal. Returns None if the PR isn't one we track.
        """
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                return None
            if review.status in (ReviewStatus.MERGED, ReviewStatus.CLOSED):
                return review
            wpid = review.wpid

        # Lock always frees; the reservation is promoted (merged) or returned to the pool (closed).
        # release() no-ops on a MERGED/absent reservation, so this is safe for update PRs too.
        if self._locks is not None:
            self._locks.release(wpid, "webhook", force=True)
        if self._allocator is not None:
            if merged:
                self._allocator.mark_merged(wpid, pr_number=pr_number)
            else:
                self._allocator.release(wpid)

        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            review.status = ReviewStatus.MERGED if merged else ReviewStatus.CLOSED
            if merged:
                review.merged_at = utcnow()
            s.commit()
            self._maybe_mirror(review)
            return review
