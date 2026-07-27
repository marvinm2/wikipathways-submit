"""Update-an-existing-pathway flow.

    check out (lock) → validate revision → branch off *latest* main → commit → open PR

Three design guarantees converge here:

- **One editor at a time (§4.3).** Acquiring the pathway lock is step one; if someone else holds
  it, or an open GitHub PR already touches the pathway (the injected ``open_pr_scanner`` on the
  registry), the update is refused. This is what makes the #90 unmergeable-conflict impossible.
- **Branch off the latest main (§5).** The revision branch is always cut from the current base
  head, so a merged PR never leaves this one rebasing across a GPML (XML) conflict.
- **The lock is held until the PR resolves.** On success the PR number is attached to the lock;
  the lock is released by webhook on PR close/merge, or by TTL. On *failure* the lock is released
  immediately so a broken attempt never freezes the pathway.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.github import BranchAlreadyExists, GitHubClient, PullRequest
from app.locks import PathwayLockRegistry
from app.submit.gpml import assign_wpid, layout_paths, validate_gpml
from app.wpid import format_wpid


class PathwayNotFound(RuntimeError):
    """Update targeted a WPID that does not exist in the repo (use submit for new pathways)."""


@dataclass(frozen=True)
class UpdateResult:
    wpid: int
    wpid_str: str
    branch: str
    path: str
    pr_number: int
    pr_url: str


class UpdateService:
    def __init__(
        self,
        locks: PathwayLockRegistry,
        github: GitHubClient,
        *,
        repo: str,
        base_branch: str = "main",
    ) -> None:
        self._locks = locks
        self._github = github
        self._repo = repo
        self._base_branch = base_branch

    def update_pathway(
        self,
        *,
        wpid: int,
        gpml: bytes | str,
        submitter: str,
        author_email: str | None = None,
        description: str | None = None,
    ) -> UpdateResult:
        # Validate before taking the lock — a malformed revision shouldn't check out the pathway.
        validate_gpml(gpml)

        # Check out the pathway (may raise LockUnavailable → surfaced as 409 upstream).
        self._locks.acquire(wpid, submitter)

        try:
            wpid_str = format_wpid(wpid)
            path = layout_paths(wpid)["gpml"]

            # The pathway must already exist; otherwise this is a submission, not an update.
            if self._github.get_file_sha(self._repo, self._base_branch, path) is None:
                raise PathwayNotFound(
                    f"{wpid_str} does not exist on {self._base_branch}; use submit for new pathways"
                )

            # Force the WPID; an update can't renumber. Author only fills in when absent, so an
            # existing pathway keeps its original authorship.
            gpml_out = assign_wpid(gpml, wpid, author=submitter)
            branch = f"update/{wpid_str}"
            base_sha = self._github.get_branch_sha(self._repo, self._base_branch)
            try:
                self._github.create_branch(self._repo, branch, base_sha)
            except BranchAlreadyExists:
                # Re-uploading while still checked out: reuse the existing update branch/PR
                # instead of opening a second PR for the same pathway.
                pass

            # SHA of the file as it currently is on the update branch (falls back to base).
            branch_file_sha = self._github.get_file_sha(self._repo, branch, path)
            self._github.put_file(
                self._repo,
                branch,
                path,
                gpml_out,
                message=f"Update {wpid_str}",
                sha=branch_file_sha,
                author_name=submitter,
                author_email=author_email,
            )
            pr: PullRequest | None = self._github.find_open_pr(self._repo, branch)
            if pr is None:
                pr = self._github.open_pull_request(
                    self._repo,
                    head=branch,
                    base=self._base_branch,
                    title=f"Update {wpid_str}",
                    body=_pr_body(wpid_str, submitter, description),
                )
        except Exception:
            # Any failure → free the pathway so it isn't stuck checked out.
            self._locks.release(wpid, submitter, force=True)
            raise

        # Keep the lock held until the PR resolves; record the PR number on it.
        self._locks.acquire(wpid, submitter, pr_number=pr.number)

        return UpdateResult(
            wpid=wpid,
            wpid_str=wpid_str,
            branch=branch,
            path=path,
            pr_number=pr.number,
            pr_url=pr.html_url,
        )


def _pr_body(wpid_str: str, submitter: str, description: str | None = None) -> str:
    body = (
        f"Automated update via wikipathways-submit.\n\n"
        f"- **WPID:** {wpid_str}\n"
        f"- **Submitter:** @{submitter}\n"
        f"- Branch cut from the latest base, so this never rebases across a GPML conflict.\n"
    )
    note = (description or "").strip()
    if note:
        body += f"\n**What changed**\n\n{note}\n"
    body += "\nThe PR-preview pipeline will render the revision and post a before/after summary."
    return body
