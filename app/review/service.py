"""Curation service (design §4.5): the reviewer's home + approve-that-merges.

Approval state is owned by the app (the ``review`` table) so the dashboard and the read-only PR
comment mirror never diverge. ``approve_and_merge`` is the one privileged action: it is gated to
the curator whitelist, requires the structured checklist to be complete, merges the PR, and then
completes the lifecycle — promoting the WPID reservation to MERGED and releasing the pathway lock.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, timedelta

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

#: The marker the target repo's publish workflow posts when it finishes (docs/sandbox-pipeline.md).
#: A comment, not the PR description, because that repo's own pipeline rewrites the description
#: wholesale and would erase anything written there.
PUBLISH_MARKER = "<!-- wikipathways-publish "
_PUBLISH_MARKER_RE = re.compile(re.escape(PUBLISH_MARKER) + r"(\{.*?\})\s*-->", re.DOTALL)

#: Review states and checklist states, in words rather than the stored enum values.
_PLAIN_STATUS = {
    "open": "waiting on review",
    "changes_requested": "waiting on the submitter to make changes",
    "approved": "approved, waiting on the repository to publish it",
    "published": "published",
    "publish_failed": "approved, but the repository never published it",
    "rejected": "rejected",
    "merged": "merged",
    "closed": "closed without merging",
}
#: Matches the pills in the dashboard and the PASS/FAIL column of the validation table that
#: sits in the same pull request, so a reader sees one vocabulary, not three.
_PLAIN_STATE = {"pass": "PASS", "fail": "FAIL", "pending": "PENDING", "na": "N/A"}

#: How many times to re-read-and-retry a checklist write that lost the optimistic-version race
#: (issue #15). Ample: contention is between a handful of curators on one review, not a hot loop.
_CHECKLIST_WRITE_RETRIES = 10


def render_mirror_comment(review: Review, repo: str, *, base_url: str = "") -> str:
    """Render the read-only PR mirror comment (design §4.5): checklist + approval state.

    Approval always flows through the app, so this comment is a *mirror* — it tells GitHub-native
    reviewers the current state and points them back to the dashboard to act.

    ``base_url`` is the app's public URL. When set, the comment links to the review page — the
    only place the before/after render exists, since CI publishes tables and pvjson but no image.
    """
    kind = "new pathway" if review.kind == "new" else "edit"
    where = _PLAIN_STATUS.get(review.status.value, review.status.value)
    assigned = (
        f" @{review.assigned_curator} is reviewing it." if review.assigned_curator else ""
    )
    lines = [
        MIRROR_MARKER,
        f"### Curation status for {review.wpid_str}",
        "",
        f"Written by the curation bot. A {kind} from @{review.submitter}, **{where}**.{assigned}",
        "",
        "| Check | State | Notes |",
        "|---|---|---|",
    ]
    for item in review.checklist:
        req = " (required)" if item.get("required") else ""
        state = _PLAIN_STATE.get(item.get("state", "pending"), item.get("state", "pending"))
        note = item.get("note") or ""
        lines.append(f"| {item['label']}{req} | {state} | {f'_{note}_' if note else ''} |")
    if review.approved_by:
        lines += ["", f"**Approved and merged by @{review.approved_by}.**"]
    if base_url:
        lines += [
            "",
            f"The pathway is drawn at {base_url.rstrip('/')}/dashboard/"
            f"{review.pr_number}. This pull request holds the GPML.",
        ]
    lines += [
        "",
        "Edits here are overwritten. The comment is generated from the curation dashboard, "
        "which is where reviewing and approving actually happen.",
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


def _aware(value):
    """Treat a stored timestamp as UTC.

    SQLite has no timezone type, so a ``DateTime(timezone=True)`` column round-trips as naive
    even though everything written to it is UTC. Comparing that to ``utcnow()`` raises. Postgres
    returns it aware, so this has to cope with both.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def parse_publish_marker(body: str) -> dict | None:
    """Extract the target repo's publish announcement from one comment body, if present.

    Structured rather than prose because the alternative — grepping for a sentence — breaks the
    moment anyone rewords it, and the repo's own pipeline rewrites text fields freely.
    """
    match = _PUBLISH_MARKER_RE.search(body or "")
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


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
        app_base_url: str = "",
        publish_mode: str = "direct",
        default_branch: str = "main",
        pipeline_workflow_file: str = "",
        label_accepted: str = "accepted",
        label_rejected: str = "rejected",
        label_author_feedback: str = "author feedback required",
        publish_timeout: timedelta = timedelta(minutes=30),
        close_rejected_after_timeout: bool = True,
        reconcile_min_interval: timedelta = timedelta(seconds=30),
        drafts=None,
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
        self._app_base_url = app_base_url
        # "direct" (we merge) vs "pipeline" (the target repo publishes). Defaulting to direct
        # keeps every existing target — wikipathways-database, a fork, the demo — unchanged.
        self._publish_mode = publish_mode
        self._default_branch = default_branch
        self._pipeline_workflow_file = pipeline_workflow_file
        self._label_accepted = label_accepted
        self._label_rejected = label_rejected
        self._label_author_feedback = label_author_feedback
        self._publish_timeout = publish_timeout
        self._close_rejected_after_timeout = close_rejected_after_timeout
        self._reconcile_min_interval = reconcile_min_interval
        # A DraftsReader, or None where the target repo publishes no draft artifacts.
        self._drafts = drafts

    @property
    def is_pipeline_mode(self) -> bool:
        return self._publish_mode == "pipeline"

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
                render_mirror_comment(review, self._repo, base_url=self._app_base_url),
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
        wpid: int | None,
        submitter: str,
        kind: str,
        metadata=None,
        before_metadata=None,
        head_branch: str | None = None,
    ) -> Review:
        """Create the review row for a freshly opened submission PR (idempotent by PR number).

        ``metadata`` (parsed from the uploaded GPML) pre-fills the checklist with auto-derived,
        curator-overridable states; ``before_metadata`` (updates only) scopes the checklist to what
        actually changed. Both optional — without them the plain all-pending checklist is used.

        ``wpid`` is None for a new pathway in pipeline mode: the id does not exist until the
        target repo assigns one. ``head_branch`` is recorded because the branch name can then no
        longer be derived from the id, and a revise has to find it again.
        """
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                review = Review(
                    pr_number=pr_number,
                    wpid=wpid,
                    submitter=submitter,
                    kind=kind,
                    head_branch=head_branch,
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
                body = f"@{curator} asked for changes before this can be merged."
                if note.strip():
                    body += f"\n\n{note.strip()}"
                body += (
                    "\n\nUpload the fixed GPML again in the curation portal. It lands on this "
                    "same pull request and puts the review back in the queue."
                )
                try:
                    self._github.create_issue_comment(self._repo, pr_number, body)
                except (GitHubError, httpx.HTTPError):
                    pass
            self._maybe_mirror(review)
            return review

    def set_checklist_item(
        self, pr_number: int, key: str, state: str, note: str | None = None
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
                            # None = "not editing the note" — a Pass/Fail/N/A click must not
                            # erase the auto-derived explanation the curator is reading.
                            if note is not None:
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

    def refresh_pipeline_checks(
        self, pr_number: int, *, gpml_reference_count: int | None = None
    ) -> Review | None:
        """Fill in checklist items from the target repo's own derived artifacts.

        That repo already resolves every data node against BridgeDb, resolves every reference
        against PubMed, and extracts the title, description and ontology tags — so most of what a
        curator has been ticking by hand is already computed and public. This reads it back.

        Two rules make it safe to run on every page load:

        - it only writes an item that is still ``pending`` and auto-derived, so a curator's
          explicit click is never overwritten by a later fetch;
        - it swallows everything, because the artifacts are often missing (that repo's PR
          workflow failed 14 of its last 20 runs, measured 2026-07-27) and a curator must still
          get a working page.
        """
        if self._drafts is None:
            return None
        from app.pipeline.drafts import datanode_check, info_checks, reference_check

        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                return None
            kind, wpid, checklist = review.kind, review.wpid, list(review.checklist)

        try:
            slug = self._drafts.slug_for(kind=kind, wpid=wpid, pr_number=pr_number)
            artifacts = self._drafts.fetch(slug)
        except Exception:  # noqa: BLE001 - a preview aid must never break the page
            return None
        if not artifacts.available:
            return None

        results: dict[str, tuple[str, str]] = {}
        for key, outcome in (
            ("datanodes_mapped", datanode_check(artifacts)),
            (
                "references_valid",
                reference_check(artifacts, gpml_reference_count=gpml_reference_count),
            ),
        ):
            if outcome is not None:
                results[key] = outcome
        results.update(info_checks(artifacts))

        review = None
        for key, (state, note) in results.items():
            current = next((i for i in checklist if i.get("key") == key), None)
            if current is None or current.get("state") != ChecklistState.PENDING.value:
                continue
            # An auto-derived `na` is only safe on an item build_checklist also marked optional.
            # Writing it onto a required item would leave something is_complete can never accept
            # — approval would be stuck until a curator noticed and overrode it by hand — so
            # downgrade to pending and let the note carry the reasoning.
            if state == ChecklistState.NA.value and current.get("required"):
                state = ChecklistState.PENDING.value
            try:
                review = self.set_checklist_item(pr_number, key, state, note=note)
            except (ReviewNotFound, ValueError, StaleDataError):
                continue
        return review

    def approve(self, pr_number: int, curator: str) -> Review:
        """Approve a submission. What that *does* depends on who owns publication.

        In direct mode it merges the PR. In pipeline mode it applies the target repo's
        ``accepted`` label and stops — that label is the whole mechanism, and the repo's own
        workflow assigns the WPID, publishes, and closes the PR unmerged. So approving is not the
        end of the story there: the review sits in APPROVED until we see the publish marker.
        """
        if not self._curators.is_curator(curator):
            raise NotACurator(f"{curator} is not on the curator whitelist")
        if self._github is None:
            raise RuntimeError("no GitHub client configured for approval")

        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                raise ReviewNotFound(f"no review for PR #{pr_number}")
            if not is_complete(review.checklist):
                raise ChecklistIncomplete(
                    f"PR #{pr_number}: required checklist items are not all passed"
                )
            wpid = review.wpid

        # Never publish a pathway whose render/validation hasn't run green (design problem #1).
        # Off by default in pipeline mode: the target repo has no pr-preview.yml, and its own
        # processing workflow fails often enough that gating on it would block every approval.
        if self._require_preview_check:
            status = self._github.pr_preview_status(
                self._repo,
                pr_number,
                workflow_file=self._preview_workflow_file,
                artifact_name=self._preview_artifact_name,
            )
            if status != "ready":
                raise PreviewNotReady(
                    f"the PR-preview check on #{pr_number} is '{status}', not 'ready'. "
                    f"Validation has to pass before this can merge."
                )

        if self.is_pipeline_mode:
            return self._approve_by_label(pr_number, curator)

        # Merge first; only mutate our state if GitHub accepts the merge.
        self._github.merge_pull_request(self._repo, pr_number)

        # Complete the lifecycle: WPID becomes permanent, pathway lock frees. Both are keyed by
        # WPID, so both are skipped when there is none yet (pipeline mode never reaches here).
        if wpid is not None:
            if self._allocator is not None:
                self._allocator.mark_merged(wpid, pr_number=pr_number)
            if self._locks is not None:
                self._locks.release(wpid, curator, force=True)

        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            review.status = ReviewStatus.MERGED
            review.approved_by = curator
            review.decided_by = curator
            review.merged_at = utcnow()
            s.commit()
            self._maybe_mirror(review)
            return review

    #: Kept so existing callers and tests keep working. In pipeline mode nothing merges, which is
    #: why the method worth calling is now ``approve``.
    approve_and_merge = approve

    def _approve_by_label(self, pr_number: int, curator: str) -> Review:
        """Hand the PR to the target repo's own publish workflow by labelling it.

        The label is applied *before* the state change and is deliberately not best-effort: if
        GitHub refuses it, nothing has been handed over, and the review has to stay reviewable
        rather than sit in APPROVED waiting for a workflow that was never triggered.
        """
        self._github.add_labels(self._repo, pr_number, [self._label_accepted])
        # The label is silent and the PR description gets rewritten by that repo's pipeline, so
        # without a comment the submitter has no way to know a curator acted.
        try:
            self._github.create_issue_comment(
                self._repo,
                pr_number,
                f"@{curator} approved this pathway in the WikiPathways curation portal.\n\n"
                "The repository's publication workflow takes it from here: it assigns the WPID, "
                "publishes the pathway, and closes this pull request without merging it. "
                "That is expected — the GPML on this branch is not what gets published.",
            )
        except (GitHubError, httpx.HTTPError):
            pass

        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            review.status = ReviewStatus.APPROVED
            review.approved_by = curator
            review.decided_by = curator
            review.approved_at = utcnow()
            s.commit()
            self._maybe_mirror(review)
            return review

    def reject(self, pr_number: int, curator: str, note: str = "") -> Review:
        """Reject a submission outright — terminal, unlike ``request_changes``.

        In pipeline mode this applies the target repo's ``rejected`` label, which triggers its
        rejection workflow: that deletes the generated drafts and closes the PR. The reason is
        commented **first**, so it is already on the record if that workflow runs immediately.
        """
        if not self._curators.is_curator(curator):
            raise NotACurator(f"{curator} is not on the curator whitelist")
        if self._github is None:
            raise RuntimeError("no GitHub client configured for rejection")

        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                raise ReviewNotFound(f"no review for PR #{pr_number}")
            wpid = review.wpid

        body = f"@{curator} rejected this submission."
        if note.strip():
            body += f"\n\n{note.strip()}"
        try:
            self._github.create_issue_comment(self._repo, pr_number, body)
        except (GitHubError, httpx.HTTPError):
            pass

        if self.is_pipeline_mode:
            self._github.add_labels(self._repo, pr_number, [self._label_rejected])
        else:
            # No pipeline to defer to; closing the PR is the rejection.
            self._github.close_pull_request(self._repo, pr_number)

        if wpid is not None:
            if self._locks is not None:
                self._locks.release(wpid, curator, force=True)
            if self._allocator is not None:
                self._allocator.release(wpid)

        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            review.status = ReviewStatus.REJECTED
            review.decided_by = curator
            review.decision_note = note.strip() or None
            s.commit()
            self._maybe_mirror(review)
            return review

    def record_published_wpid(self, pr_number: int, wpid: int, curator: str) -> Review:
        """Escape hatch: a curator records the WPID the target repo assigned.

        Needed because that repo's publish workflow is the one part of the loop we do not
        control. When it fails to announce — or fails outright and someone publishes by hand —
        this is how the review still reaches a truthful terminal state.
        """
        if not self._curators.is_curator(curator):
            raise NotACurator(f"{curator} is not on the curator whitelist")
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                raise ReviewNotFound(f"no review for PR #{pr_number}")
            review.wpid = wpid
            review.status = ReviewStatus.PUBLISHED
            review.published_at = utcnow()
            review.decision_note = f"WPID recorded by @{curator}"
            s.commit()
            self._maybe_mirror(review)
            return review

    def _published_wpid(self, pr_number: int) -> int | None:
        """Read the WPID out of the target repo's publish marker comment, if it posted one."""
        if self._github is None:
            return None
        try:
            bodies = self._github.list_issue_comments(self._repo, pr_number)
        except (GitHubError, httpx.HTTPError):
            return None
        for body in reversed(bodies):
            payload = parse_publish_marker(body)
            if payload and payload.get("status") == "published":
                try:
                    return int(payload["wpid"])
                except (KeyError, TypeError, ValueError):
                    continue
        return None

    def _wpid_is_on_main(self, wpid: int) -> bool:
        """Confirm the pathway really landed before we call a review published."""
        if self._github is None:
            return False
        try:
            content = self._github.get_file_content(
                self._repo, self._default_branch, f"pathways/WP{wpid}/WP{wpid}.gpml"
            )
        except (GitHubError, httpx.HTTPError):
            return False
        return content is not None

    #: Statuses no further automatic transition applies to.
    _TERMINAL = (
        ReviewStatus.MERGED,
        ReviewStatus.CLOSED,
        ReviewStatus.PUBLISHED,
        ReviewStatus.REJECTED,
    )

    def handle_pr_closed(self, pr_number: int, *, merged: bool) -> Review | None:
        """React to a PR closed/merged **outside** the app (webhook, issue #8).

        Frees the pathway lock, finalises the WPID reservation (permanent if merged, returned to
        the pool if closed unmerged), and moves the review to a terminal state. Idempotent: a
        duplicate delivery — or the webhook for a merge the app itself performed — is a no-op
        because the review is already terminal. Returns None if the PR isn't one we track.

        A close **without** a merge is not automatically a failure any more. On a target repo
        that publishes through its own Actions, that is exactly what a successful publication
        looks like, so an approved review takes the publication branch instead.
        """
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                return None
            if review.status in self._TERMINAL:
                return review
            approved = review.status == ReviewStatus.APPROVED
            wpid = review.wpid

        if approved and not merged:
            return self._settle_publication(pr_number)

        # Lock always frees; the reservation is promoted (merged) or returned to the pool (closed).
        # release() no-ops on a MERGED/absent reservation, so this is safe for update PRs too.
        # A review with no WPID yet holds neither a lock nor a reservation, so there is nothing
        # to free — that is the whole point of assigning the id at publication.
        if wpid is not None:
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

    def _settle_publication(self, pr_number: int) -> Review | None:
        """Decide what an approved-then-closed PR actually means, and record it.

        The target repo's publish workflow announces the assigned WPID in a marker comment. If
        that announcement is there and the pathway is really on ``main``, the submission is
        published. If the PR closed with nothing to show, it is not — and saying so is the point,
        because that workflow has failed far more often than it has succeeded.
        """
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                return None
            wpid = review.wpid

        published_wpid = self._published_wpid(pr_number) or wpid
        note: str | None = None
        if published_wpid is None:
            status = ReviewStatus.PUBLISH_FAILED
            note = (
                "The pull request was closed without the repository announcing a WPID, so the "
                "pathway was almost certainly not published. Check the repository's publish "
                "workflow, then record the WPID here once you know it."
            )
        else:
            status = ReviewStatus.PUBLISHED
            if not self._wpid_is_on_main(published_wpid):
                # Not a failure: the publish workflow pushes to main directly and our read can
                # simply be early. Worth saying out loud rather than silently asserting success.
                note = (
                    f"WP{published_wpid} was announced but is not visible on "
                    f"{self._default_branch} yet."
                )

        if wpid is not None and self._locks is not None:
            self._locks.release(wpid, "pipeline", force=True)

        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            review.status = status
            review.decision_note = note
            review.last_checked_at = utcnow()
            if status == ReviewStatus.PUBLISHED:
                review.wpid = published_wpid
                review.published_at = utcnow()
            s.commit()
            self._maybe_mirror(review)
            return review

    def _pipeline_run_state(self, pr_number: int) -> dict | None:
        """Last-seen state of the target repo's PR-processing workflow for this pull request.

        Its failure is worth surfacing on its own: when that repo cannot read a submitted GPML
        the run dies early, the submitter silently loses their metadata tables and their draft
        page, and the only trace is a job several clicks into the Actions tab. Best-effort — this
        is a diagnostic, and never a reason to fail a reconcile.

        Keyed on the pull request's head SHA, so it reports the run *this* revision triggered. A
        run someone dispatched by hand carries the default branch's SHA and no reference to any
        pull request, so it is invisible here — deliberately, since there is no reliable way to
        join one to a review. In the ordinary flow that costs nothing: a revise pushes a new
        commit, which starts a fresh run against the new head, and this follows it.
        """
        if not self.is_pipeline_mode or not self._pipeline_workflow_file:
            return None
        try:
            run = self._github.latest_workflow_run_for_pr(
                self._repo, pr_number, workflow_file=self._pipeline_workflow_file
            )
        except (GitHubError, httpx.HTTPError):
            return None
        if run is None:
            return None
        return {"status": run.status, "conclusion": run.conclusion, "url": run.html_url}

    def _reconcile_one(self, pr_number: int) -> bool:
        """Bring one review in line with GitHub. Returns True if its status changed."""
        try:
            detail = self._github.get_pull_request(self._repo, pr_number)
        except (GitHubError, httpx.HTTPError):
            return False  # transient — leave it; try again next load

        pipeline_run = self._pipeline_run_state(pr_number) if detail is not None else None

        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                return False
            status, approved_at = review.status, review.approved_at
            review.last_checked_at = utcnow()
            if detail is not None:
                review.github_labels = list(detail.labels)
            if pipeline_run is not None:
                review.pipeline_run = pipeline_run
            s.commit()

        if detail is None:
            # Deleted or inaccessible. For an approved review that is a failure to publish, not
            # a quiet close: the pathway's fate is unknown and someone has to look.
            if status == ReviewStatus.APPROVED:
                return self._fail_publication(
                    pr_number, "The pull request no longer exists on GitHub."
                )
            self.handle_pr_closed(pr_number, merged=False)
            return True

        if detail.state != "open":
            self.handle_pr_closed(pr_number, merged=detail.merged)
            return True

        if status != ReviewStatus.APPROVED:
            return False

        # Still approved and still open: either somebody took the label back off, or the target
        # repo's workflow never fired. Both need saying; neither is visible anywhere else.
        if self._label_accepted not in detail.labels:
            with self._session_factory() as s:
                review = s.get(Review, pr_number)
                review.status = ReviewStatus.OPEN
                review.decision_note = "The accepted label was removed on GitHub."
                s.commit()
                self._maybe_mirror(review)
            return True
        approved_at = _aware(approved_at)
        if approved_at is not None and utcnow() - approved_at > self._publish_timeout:
            waited = int((utcnow() - approved_at).total_seconds() // 60)
            return self._fail_publication(
                pr_number,
                f"The accepted label has been on this pull request for {waited} minutes and "
                f"{self._repo} has not published it. Re-run the repository's publish workflow, "
                "then record the assigned WPID here.",
            )
        return False

    def _fail_publication(self, pr_number: int, note: str) -> bool:
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                return False
            review.status = ReviewStatus.PUBLISH_FAILED
            review.decision_note = note
            s.commit()
            self._maybe_mirror(review)
        return True

    def reconcile_review(self, pr_number: int) -> bool:
        """Bring a single review in line with GitHub, honouring the same throttle.

        The queue reconciles everything on load, but a review page reached directly — a link in
        a comment, a bookmark, a page refresh after acting — would otherwise render from whatever
        the last queue load happened to record, and show no upstream failure at all.
        """
        if self._github is None:
            return False
        cutoff = utcnow() - self._reconcile_min_interval
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None or review.status in self._TERMINAL:
                return False
            last = _aware(review.last_checked_at)
        if last is not None and last > cutoff:
            return False
        return self._reconcile_one(pr_number)

    def reconcile(self) -> int:
        """Bring every non-terminal review in line with GitHub (issue #1).

        A PR closed or merged *outside* the app — a raw merge, a manual close, a webhook that
        never arrived (as in the demo, which wires none) — would otherwise linger forever. This
        runs on each dashboard load, so it also covers approved reviews waiting on the target
        repo's publish workflow, which is where they now accumulate.

        Each review is re-checked at most once per ``reconcile_min_interval``. Without that,
        approved reviews stuck behind a broken publish workflow would turn every dashboard load
        into one GitHub request per stuck review, forever.
        """
        if self._github is None:
            return 0
        cutoff = utcnow() - self._reconcile_min_interval
        with self._session_factory() as s:
            due = [
                r.pr_number
                for r in s.execute(
                    select(Review).where(Review.status.notin_(self._TERMINAL))
                ).scalars()
                if _aware(r.last_checked_at) is None or _aware(r.last_checked_at) <= cutoff
            ]
        return sum(1 for pr_number in due if self._reconcile_one(pr_number))

    #: Older name, kept so existing callers keep working.
    reconcile_open_reviews = reconcile

    def handle_label_event(
        self, pr_number: int, label: str, *, added: bool, actor: str
    ) -> Review | None:
        """Mirror a label a human applied on GitHub directly (webhook).

        The labels are the target repo's native vocabulary, so curators will reach for them there
        as well as in the dashboard. Neither the whitelist nor the checklist is enforced here:
        GitHub is reporting something that already happened, and the app's job is to record it —
        and to say so in the note when it bypassed a gate.
        """
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            if review is None:
                return None
            status = review.status
            complete = is_complete(review.checklist)

        if added and label == self._label_accepted:
            if status not in (ReviewStatus.OPEN, ReviewStatus.CHANGES_REQUESTED):
                return None  # already approved, or past it — the app's own label echoes here
            note = None if complete else "Approved on GitHub with an incomplete checklist."
            return self._set_status(
                pr_number,
                ReviewStatus.APPROVED,
                actor=actor,
                note=note,
                approved_at=utcnow(),
            )
        if added and label == self._label_rejected:
            if status in self._TERMINAL:
                return None
            return self._set_status(
                pr_number, ReviewStatus.REJECTED, actor=actor, note="Rejected on GitHub."
            )
        if not added and label == self._label_accepted and status == ReviewStatus.APPROVED:
            return self._set_status(
                pr_number,
                ReviewStatus.OPEN,
                actor=None,
                note="The accepted label was removed on GitHub.",
            )
        return None

    def _set_status(
        self,
        pr_number: int,
        status: ReviewStatus,
        *,
        actor: str | None,
        note: str | None,
        approved_at=None,
    ) -> Review:
        with self._session_factory() as s:
            review = s.get(Review, pr_number)
            review.status = status
            review.decision_note = note
            if actor is not None:
                review.decided_by = actor
                if status == ReviewStatus.APPROVED:
                    review.approved_by = actor
            if approved_at is not None:
                review.approved_at = approved_at
            s.commit()
            self._maybe_mirror(review)
            return review
