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

from app.github import GitHubClient, PullRequest
from app.submit.gpml import assign_wpid, layout_paths, validate_gpml
from app.wpid import WpidAllocator, format_wpid


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
    ) -> SubmissionResult:
        # 1. Validate first — a malformed upload must not consume a WPID.
        meta = validate_gpml(gpml)

        # 2. Reserve the WPID atomically.
        wpid = self._allocator.allocate(submitter)

        try:
            # 3. Stamp the assigned WPID into the GPML (Version attribute).
            gpml_out = assign_wpid(gpml, wpid)
            path = layout_paths(wpid)["gpml"]
            wpid_str = format_wpid(wpid)
            branch = f"submit/{wpid_str}"

            # 4. Branch off the latest base, 5. commit the file, 6. open the PR.
            base_sha = self._github.get_branch_sha(self._repo, self._base_branch)
            self._github.create_branch(self._repo, branch, base_sha)
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
                body=_pr_body(wpid_str, meta.name, meta.organism, submitter),
            )
        except Exception:
            # Any GitHub failure → return the WPID to the pool before re-raising.
            self._allocator.release(wpid)
            raise

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


def _pr_body(wpid_str: str, name: str | None, organism: str | None, submitter: str) -> str:
    return (
        f"Automated submission via wikipathways-submit.\n\n"
        f"- **Pathway:** {name or '(unnamed)'}\n"
        f"- **WPID:** {wpid_str} (assigned by the app)\n"
        f"- **Organism:** {organism or '(unset)'}\n"
        f"- **Submitter:** @{submitter}\n\n"
        f"The PR-preview pipeline will render this pathway and post a validation summary."
    )
