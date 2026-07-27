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

from dataclasses import dataclass

from app.github import BranchAlreadyExists, GitHubClient, GitHubError, PullRequest
from app.submit.gpml import assign_wpid, layout_paths, validate_gpml
from app.wpid import WpidAllocator, format_wpid

#: How many times to step past a WPID whose ``submit/WP<id>`` branch already exists.
_BRANCH_COLLISION_RETRIES = 3


class NoPendingSubmission(RuntimeError):
    """Revise targeted a WPID with no open new-pathway submission branch/PR to commit onto."""


@dataclass(frozen=True)
class SubmissionResult:
    wpid: int
    wpid_str: str
    branch: str
    path: str
    pr_number: int
    pr_url: str


class SubmissionService:
    def __init__(
        self,
        allocator: WpidAllocator,
        github: GitHubClient,
        *,
        repo: str,
        base_branch: str = "main",
    ) -> None:
        self._allocator = allocator
        self._github = github
        self._repo = repo
        self._base_branch = base_branch

    def submit_new_pathway(
        self,
        *,
        gpml: bytes | str,
        submitter: str,
        author_email: str | None = None,
        description: str | None = None,
    ) -> SubmissionResult:
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
        f"Automated submission via wikipathways-submit.\n\n"
        f"- **Pathway:** {name or '(unnamed)'}\n"
        f"- **WPID:** {wpid_str} (assigned by the app)\n"
        f"- **Organism:** {organism or '(unset)'}\n"
        f"- **Submitter:** @{submitter}\n"
    )
    note = (description or "").strip()
    if note:
        body += f"\n**Submitter note**\n\n{note}\n"
    body += (
        "\nThe PR-preview workflow will post a validation and metadata summary here. "
        "The pathway itself is drawn in the curation dashboard, not in this pull request."
    )
    return body
