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
from sqlalchemy.orm.exc import StaleDataError

from app.curators import CuratorRegistry
from app.github import GitHubClient, GitHubError
from app.locks import PathwayLockRegistry
from app.models import Review, ReviewStatus, utcnow
from app.review.checklist import ChecklistState, build_checklist, is_complete, is_valid_key
from app.wpid import WpidAllocator

#: Hidden token embedded in the mirror comment so we update the same one instead of spamming.
MIRROR_MARKER = "<!-- wikipathways-submit:mirror -->"

#: How many times to re-read-and-retry a checklist write that lost the optimistic-version race
#: (issue #15). Ample: contention is between a handful of curators on one review, not a hot loop.
_CHECKLIST_WRITE_RETRIES = 10


def render_mirror_comment(review: Review, repo: str) -> str:
    """Render the read-only PR mirror comment (design §4.5): checklist + approval state.

    Approval always flows through the app, so this comment is a *mirror* — it tells GitHub-native
    reviewers the current state and points them back to the dashboard to act.
    """
    lines = [
        MIRROR_MARKER,
        f"### 🤖 WikiPathways curation — WP{review.wpid}",
        "_Automated message from the curation bot._",
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


class PreviewNotReady(RuntimeError):
    """Approval attempted before the PR-preview CI workflow has completed successfully."""


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
        require_preview_check: bool = False,
        preview_workflow_file: str = "",
        preview_artifact_name: str = "",
    ) -> None:
        self._session_factory = session_factory
        self._github = github
        self._repo = repo
        self._curators = curators
        self._allocator = allocator
        self._locks = locks
        self._require_preview_check = require_preview_check
        self._preview_workflow_file = preview_workflow_file
        self._preview_artifact_name = preview_artifact_name

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

    def register(
        self,
        *,
        pr_number: int,
        wpid: int,
        submitter: str,
        kind: str,
        metadata=None,
        before_metadata=None,
    ) -> Review:
        """Create the review row for a freshly opened submission PR (idempotent by PR number).

        ``metadata`` (parsed from the uploaded GPML) pre-fills the checklist with auto-derived,
        curator-overridable states; ``before_metadata`` (updates only) scopes the checklist to what
        actually changed. Both optional — without them the plain all-pending checklist is used.
        """
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                review = Review(
                    pr_number=pr_number,
                    wpid=wpid,
                    submitter=submitter,
                    kind=kind,
                    checklist=build_checklist(
                        metadata=metadata, before=before_metadata, kind=kind
                    ),
                )
                s.add(review)
                s.commit()
            elif review.status == ReviewStatus.CHANGES_REQUESTED:
                # A re-upload after changes were requested puts it back in the review queue.
                review.status = ReviewStatus.OPEN
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
            self._maybe_request_reviewer(pr_number, curator)
            return review

    def _maybe_request_reviewer(self, pr_number: int, curator: str) -> None:
        """Best-effort: mirror the app-side assignment as a real PR review request on GitHub.

        GitHub refuses to request a review from the PR author (the submitter reviewing their own
        pathway) or from a non-collaborator, and returns 422. That must not fail the app-side
        assignment — the dashboard is the source of truth — so the error is swallowed.
        """
        if self._github is None or not curator:
            return
        try:
            self._github.request_pr_reviewer(self._repo, pr_number, curator)
        except (GitHubError, httpx.HTTPError):
            pass

    def find_open_new_review(self, wpid: int) -> Review | None:
        """The still-open (or changes-requested) new-pathway review for ``wpid``, if any — used to
        route a re-upload of that WPID to the revise flow."""
        with self._session_factory() as s:
            return s.execute(
                select(Review).where(
                    Review.wpid == wpid,
                    Review.kind == "new",
                    Review.status.in_([ReviewStatus.OPEN, ReviewStatus.CHANGES_REQUESTED]),
                )
            ).scalars().first()

    def revise(self, pr_number: int, metadata=None) -> Review:
        """A revision landed on a review's PR: re-open it and rebuild the checklist from the new
        metadata, so the curator re-reviews the changed content from a fresh auto-derived baseline.
        """
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                raise ReviewNotFound(f"no review for PR #{pr_number}")
            review.status = ReviewStatus.OPEN
            review.checklist = build_checklist(metadata=metadata, kind=review.kind)
            s.commit()
            self._maybe_mirror(review)
            return review

    def request_changes(self, pr_number: int, curator: str, note: str = "") -> Review:
        """Ask the submitter to revise: move the review to CHANGES_REQUESTED and post the note as a
        PR comment so it reaches them on GitHub. The lock/reservation stay held — the PR is still
        open and a re-upload re-opens the review (see ``register``). Comment is best-effort."""
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                raise ReviewNotFound(f"no review for PR #{pr_number}")
            review.status = ReviewStatus.CHANGES_REQUESTED
            s.commit()
            if self._github is not None:
                body = f"**Changes requested** by @{curator}."
                if note.strip():
                    body += f"\n\n{note.strip()}"
                body += "\n\nRe-upload to revise — it reuses this PR and re-opens the review."
                try:
                    self._github.create_issue_comment(self._repo, pr_number, body)
                except (GitHubError, httpx.HTTPError):
                    pass
            self._maybe_mirror(review)
            return review

    def set_checklist_item(
        self, pr_number: int, key: str, state: str, note: str = ""
    ) -> Review:
        if not is_valid_key(key):
            raise ValueError(f"unknown checklist item: {key}")
        if state not in {s.value for s in ChecklistState}:
            raise ValueError(f"invalid checklist state: {state}")
        # The checklist is one JSON blob on the review row, so setting an item is a
        # read-modify-write of the whole list. Two curators — or a burst of clicks — updating
        # *different* items at once would otherwise lose updates: each reads the list, changes
        # its own item, writes the whole list back, and the last commit wins, silently dropping
        # the others (issue #15). Review carries a ``version_id_col``, so the ORM stamps every
        # UPDATE with ``WHERE version = <read value>`` and raises StaleDataError when a concurrent
        # write got there first. On that conflict we re-read the fresh row and retry — the same
        # read-latest-and-retry shape the allocator uses, and correct on both Postgres and SQLite.
        for attempt in range(_CHECKLIST_WRITE_RETRIES):
            try:
                with self._session_factory() as s:
                    review = s.get(Review, pr_number)
                    if review is None:
                        raise ReviewNotFound(f"no review for PR #{pr_number}")
                    # Rebuild the list (new object) so the JSON column is marked dirty.
                    checklist = [dict(item) for item in review.checklist]
                    for item in checklist:
                        if item["key"] == key:
                            item["state"] = state
                            item["note"] = note
                    review.checklist = checklist
                    s.commit()  # version-guarded UPDATE; StaleDataError if we lost the race
                    self._maybe_mirror(review)
                    return review
            except StaleDataError:
                # Another checklist write committed between our read and write. Retry with a
                # fresh read so its change is preserved and ours is layered on top.
                if attempt == _CHECKLIST_WRITE_RETRIES - 1:
                    raise
        raise AssertionError("unreachable: the retry loop always returns or raises")

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

        # Never merge a pathway whose render/validation hasn't run green (design problem #1): the
        # PR-preview CI workflow must have completed successfully before we merge.
        if self._require_preview_check:
            status = self._github.pr_preview_status(
                self._repo,
                pr_number,
                workflow_file=self._preview_workflow_file,
                artifact_name=self._preview_artifact_name,
            )
            if status != "ready":
                raise PreviewNotReady(
                    f"PR #{pr_number}: PR-preview check is '{status}', not 'ready' — the render "
                    f"and validation must pass before this can be merged"
                )

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

    def reconcile_open_reviews(self) -> int:
        """Terminalise open reviews whose PR is no longer open on GitHub (issue #1).

        A PR closed or merged *outside* the app — a raw merge, a manual close, or a webhook that
        never arrived (as in the demo, which wires none) — would otherwise linger in the queue
        forever. On each dashboard load we ask GitHub for the real state of every open review's PR
        and, for any that is merged/closed/gone, run the same finalisation the webhook would. A
        missing PR (deleted, 404) counts as closed-unmerged. Best-effort per review and a no-op if
        no GitHub client is configured. Returns how many were reconciled.
        """
        if self._github is None:
            return 0
        with self._session_factory() as s:
            open_prs = [
                r.pr_number
                for r in s.execute(
                    select(Review).where(Review.status == ReviewStatus.OPEN)
                ).scalars()
            ]
        reconciled = 0
        for pr_number in open_prs:
            try:
                state = self._github.get_pull_request_state(self._repo, pr_number)
            except GitHubError:
                continue  # transient — leave the review; try again next load
            if state == "open":
                continue
            # merged → promote; closed or absent (None) → return the id to the pool
            self.handle_pr_closed(pr_number, merged=(state == "merged"))
            reconciled += 1
        return reconciled
