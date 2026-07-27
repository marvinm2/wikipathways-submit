"""New-pathway submission service (design §4.1).

Orchestrates the new-pathway flow end to end:

    validate GPML → reserve WPID (atomic) → write WPID into the GPML → create a branch →
    commit the file → open a PR → record the PR number on the reservation.

Two correctness properties:

- **A failed submission burns no WPID.** If any GitHub step fails after the WPID was reserved,
  the reservation is released (id returned to the pool) before the error propagates. (Reservations
  also expire by TTL, but immediate release keeps the sequence tight.)
- **Validation happens before allocation**, so a malformed upload never consumes an id at all.
"""
from __future__ import annotations

import enum
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.github import BranchAlreadyExists, GitHubClient, GitHubError, PullRequest
from app.submit.gpml import (
    PLACEHOLDER_GPML_PATH,
    PLACEHOLDER_WPID_STR,
    assign_wpid,
    assign_wpid_str,
    layout_paths,
    validate_gpml,
)
from app.wpid import WpidAllocator, format_wpid

#: How many times to step past a WPID whose ``submit/WP<id>`` branch already exists.
_BRANCH_COLLISION_RETRIES = 3

#: Characters a GitHub login cannot contain, so a branch name built from one stays valid.
_UNSAFE_IN_BRANCH = re.compile(r"[^A-Za-z0-9._-]")


class SubmissionMode(enum.StrEnum):
    #: The app owns publication: allocate a WPID up front, branch ``submit/WP<id>``, file at
    #: ``pathways/WP<id>/WP<id>.gpml``, and approving merges the PR.
    DIRECT = "direct"
    #: The target repo owns publication (docs/sandbox-pipeline.md): no WPID is claimed, the
    #: branch is named for the submitter and a timestamp, and the file carries a placeholder
    #: until the repo's own workflow assigns the real id at publication.
    PIPELINE = "pipeline"


class NoPendingSubmission(RuntimeError):
    """Revise targeted a WPID with no open new-pathway submission branch/PR to commit onto."""


@dataclass(frozen=True)
class SubmissionResult:
    #: None in pipeline mode — the id does not exist until the target repo publishes.
    wpid: int | None
    wpid_str: str
    branch: str
    path: str
    pr_number: int
    pr_url: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SubmissionService:
    def __init__(
        self,
        allocator: WpidAllocator | None,
        github: GitHubClient,
        *,
        repo: str,
        base_branch: str = "main",
        mode: SubmissionMode = SubmissionMode.DIRECT,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._allocator = allocator
        self._github = github
        self._repo = repo
        self._base_branch = base_branch
        self._mode = mode
        # Injected so the timestamped branch name is deterministic under test.
        self._clock = clock

    def submit_new_pathway(
        self,
        *,
        gpml: bytes | str,
        submitter: str,
        author_email: str | None = None,
        description: str | None = None,
    ) -> SubmissionResult:
        if self._mode is SubmissionMode.PIPELINE:
            return self._submit_pipeline(
                gpml=gpml,
                submitter=submitter,
                author_email=author_email,
                description=description,
            )
        return self._submit_direct(
            gpml=gpml, submitter=submitter, author_email=author_email, description=description
        )

    def _submit_direct(
        self,
        *,
        gpml: bytes | str,
        submitter: str,
        author_email: str | None = None,
        description: str | None = None,
    ) -> SubmissionResult:
        if self._allocator is None:
            raise RuntimeError("direct submission needs a WPID allocator")
        # 1. Validate first — a malformed upload must not consume a WPID.
        meta = validate_gpml(gpml)

        # 2. Reserve the WPID atomically and claim its branch. A PR closed unmerged leaves its
        # branch behind, so a free id can still have a taken branch name; the floor counts those
        # branches, and this loop steps past any that slipped through (e.g. a branch created
        # between the floor read and here). Colliding reservations are held until a clean id is
        # found — releasing one first would just hand the same id back — then returned to the pool.
        collided: list[int] = []
        try:
            for attempt in range(_BRANCH_COLLISION_RETRIES):
                wpid = self._allocator.allocate(submitter)

                # 3. Stamp the assigned WPID into the GPML (Version attribute), and the
                #    submitter as Author if the upload carries none.
                gpml_out = assign_wpid(gpml, wpid, author=submitter)
                path = layout_paths(wpid)["gpml"]
                wpid_str = format_wpid(wpid)
                branch = f"submit/{wpid_str}"

                # 4. Branch off the latest base.
                try:
                    base_sha = self._github.get_branch_sha(self._repo, self._base_branch)
                    self._github.create_branch(self._repo, branch, base_sha)
                except BranchAlreadyExists:
                    collided.append(wpid)
                    if attempt == _BRANCH_COLLISION_RETRIES - 1:
                        raise
                    continue
                except Exception:
                    self._allocator.release(wpid)
                    raise
                break

            try:
                # 5. Commit the file, 6. open the PR.
                self._github.put_file(
                    self._repo,
                    branch,
                    path,
                    gpml_out,
                    message=f"Add {wpid_str}: {meta.name}",
                    author_name=submitter,
                    author_email=author_email,
                )
                pr: PullRequest = self._github.open_pull_request(
                    self._repo,
                    head=branch,
                    base=self._base_branch,
                    title=f"New pathway {wpid_str}: {meta.name}",
                    body=_pr_body(wpid_str, meta.name, meta.organism, submitter, description),
                )
            except Exception:
                # Any GitHub failure → return the WPID to the pool before re-raising.
                self._allocator.release(wpid)
                raise
        finally:
            for burnt in collided:
                self._allocator.release(burnt)

        # 7. Record the PR number on the reservation.
        self._allocator.attach_pr(wpid, pr.number)

        return SubmissionResult(
            wpid=wpid,
            wpid_str=wpid_str,
            branch=branch,
            path=path,
            pr_number=pr.number,
            pr_url=pr.html_url,
        )

    def _pipeline_branch(self, submitter: str, *, suffix: int = 1) -> str:
        """``WP0001_<submitter>_<YYYYMMDD-HHMMSS>`` — placeholder id, who, and when.

        Id-then-user matches the branches the target repo already carries; the readable stamp
        beats an epoch-ms blob when a curator is scanning a branch list. The stamp is also what
        makes the name unique, so a leftover branch from a closed PR can no longer block the
        next submission the way a WPID-derived name did.
        """
        safe = _UNSAFE_IN_BRANCH.sub("-", submitter) or "user"
        stamp = self._clock().astimezone(UTC).strftime("%Y%m%d-%H%M%S")
        base = f"{PLACEHOLDER_WPID_STR}_{safe}_{stamp}"
        return base if suffix == 1 else f"{base}-{suffix}"

    def _submit_pipeline(
        self,
        *,
        gpml: bytes | str,
        submitter: str,
        author_email: str | None = None,
        description: str | None = None,
    ) -> SubmissionResult:
        """Open a PR carrying the placeholder, claiming no WPID.

        There is no rollback path here and nothing to roll back: no id is reserved, so a failed
        submission cannot burn one. That is the whole reason for assigning the id at publication.
        """
        meta = validate_gpml(gpml)

        # The target repo classifies new-vs-edit from the *basename* alone, so the placeholder
        # has to be a name its "existing WPID" pattern rejects. WP0001 does that (a leading
        # zero), which is why the id and the path are not free choices — see
        # tests/test_pipeline_submission.py, which pins that repo's regex.
        gpml_out = assign_wpid_str(gpml, PLACEHOLDER_WPID_STR, author=submitter)
        path = PLACEHOLDER_GPML_PATH

        base_sha = self._github.get_branch_sha(self._repo, self._base_branch)
        for attempt in range(1, _BRANCH_COLLISION_RETRIES + 1):
            branch = self._pipeline_branch(submitter, suffix=attempt)
            try:
                self._github.create_branch(self._repo, branch, base_sha)
                break
            except BranchAlreadyExists:
                # Same person, same second. Rare, and cheap to step past.
                if attempt == _BRANCH_COLLISION_RETRIES:
                    raise

        self._github.put_file(
            self._repo,
            branch,
            path,
            gpml_out,
            message=f"Add {meta.name or 'new pathway'}",
            author_name=submitter,
            author_email=author_email,
        )
        pr: PullRequest = self._github.open_pull_request(
            self._repo,
            head=branch,
            base=self._base_branch,
            title=f"[NEW] {meta.name or 'Untitled pathway'} — submitted by @{submitter}",
            body=_pipeline_pr_body(meta.name, meta.organism, submitter, description),
        )
        return SubmissionResult(
            wpid=None,
            wpid_str=PLACEHOLDER_WPID_STR,
            branch=branch,
            path=path,
            pr_number=pr.number,
            pr_url=pr.html_url,
        )

    def revise_new_pathway(
        self,
        *,
        wpid: int,
        gpml: bytes | str,
        submitter: str,
        author_email: str | None = None,
    ) -> SubmissionResult:
        """Commit a revised GPML onto an **already-open** new-pathway PR (design: the revise loop).

        Unlike ``submit_new_pathway`` this reserves no WPID and opens no PR — it adds a commit to
        the existing ``submit/WP<id>`` branch, reusing the open PR. Used when a curator requested
        changes on a new submission (which, unlike an update, isn't on ``main`` yet). Raises
        ``NoPendingSubmission`` if there is no open submission branch/PR for the WPID.
        """
        validate_gpml(gpml)
        wpid_str = format_wpid(wpid)
        path = layout_paths(wpid)["gpml"]
        branch = f"submit/{wpid_str}"

        try:
            self._github.get_branch_sha(self._repo, branch)
        except GitHubError as exc:
            raise NoPendingSubmission(
                f"no open submission branch for {wpid_str} to revise"
            ) from exc
        pr = self._github.find_open_pr(self._repo, branch)
        if pr is None:
            raise NoPendingSubmission(f"no open submission PR for {wpid_str} to revise")

        # Keep the assigned WPID; a revise can't renumber.
        gpml_out = assign_wpid(gpml, wpid, author=submitter)
        branch_file_sha = self._github.get_file_sha(self._repo, branch, path)
        self._github.put_file(
            self._repo,
            branch,
            path,
            gpml_out,
            message=f"Revise {wpid_str}",
            sha=branch_file_sha,
            author_name=submitter,
            author_email=author_email,
        )
        return SubmissionResult(
            wpid=wpid,
            wpid_str=wpid_str,
            branch=branch,
            path=path,
            pr_number=pr.number,
            pr_url=pr.html_url,
        )


def _pr_body(
    wpid_str: str,
    name: str | None,
    organism: str | None,
    submitter: str,
    description: str | None = None,
) -> str:
    body = (
        f"@{submitter} submitted this pathway through the WikiPathways curation portal.\n\n"
        f"- **Pathway:** {name or '(unnamed)'}\n"
        f"- **WPID:** {wpid_str}, assigned by the app\n"
        f"- **Organism:** {organism or '(unset)'}\n"
    )
    note = (description or "").strip()
    if note:
        body += f"\n**Note from the submitter**\n\n{note}\n"
    body += "\nThe drawn pathway is in the curation dashboard; this pull request holds the GPML."
    return body


def _pipeline_pr_body(
    name: str | None,
    organism: str | None,
    submitter: str,
    description: str | None = None,
) -> str:
    """Short on purpose: the target repo's own workflow overwrites this description twice.

    It is worth writing anyway — it is what a person sees in the minute before that workflow
    starts, and it is all there is if the workflow never runs. The durable record is the mirror
    comment, which no workflow touches.
    """
    body = (
        f"@{submitter} submitted this pathway through the WikiPathways curation portal.\n\n"
        f"- **Pathway:** {name or '(unnamed)'}\n"
        f"- **Organism:** {organism or '(unset)'}\n"
        f"- **WPID:** not yet assigned — a curator's approval is what assigns it\n"
    )
    note = (description or "").strip()
    if note:
        body += f"\n**Note from the submitter**\n\n{note}\n"
    body += (
        "\nThis description is regenerated by the repository's own pipeline. "
        "The curation record lives in the pinned comment below.\n"
    )
    return body
