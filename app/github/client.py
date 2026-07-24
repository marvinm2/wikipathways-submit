"""GitHub client: the minimal surface the submission flow needs, plus a fake and an httpx impl.

The submission flow (open a PR that adds one GPML file) needs exactly four operations:
resolve a branch's head SHA, create a branch, create a file on it, and open a PR. Keeping the
interface this small makes the fake trivial and keeps the real client honest.
"""
from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

_GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class PullRequest:
    number: int
    html_url: str
    head_branch: str


class GitHubError(RuntimeError):
    """A GitHub operation failed."""


class BranchAlreadyExists(GitHubError):
    """The branch to be created already exists (e.g. a resubmission collided)."""


class GitHubClient(ABC):
    @abstractmethod
    def get_branch_sha(self, repo: str, branch: str) -> str:
        """Return the head commit SHA of ``branch`` in ``owner/repo``."""

    @abstractmethod
    def create_branch(self, repo: str, new_branch: str, from_sha: str) -> None:
        """Create ``new_branch`` at ``from_sha``; raises BranchAlreadyExists on conflict."""

    @abstractmethod
    def get_file_sha(self, repo: str, ref: str, path: str) -> str | None:
        """Return the blob SHA of ``path`` at ``ref`` (branch/sha), or None if absent."""

    @abstractmethod
    def put_file(
        self,
        repo: str,
        branch: str,
        path: str,
        content: str,
        message: str,
        *,
        sha: str | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> None:
        """Create or update a text file on ``branch``.

        ``sha`` is the current blob SHA of the file and is **required to update** an existing
        file (GitHub rejects an update without it); omit it to create a new file.
        """

    @abstractmethod
    def find_open_pr(self, repo: str, head_branch: str) -> PullRequest | None:
        """Return the open PR whose head is ``head_branch``, or None."""

    @abstractmethod
    def open_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        """Open a PR from ``head`` into ``base`` and return it."""

    @abstractmethod
    def merge_pull_request(self, repo: str, pr_number: int, *, method: str = "squash") -> None:
        """Merge a PR. Raises GitHubError if the merge is not allowed."""


class FakeGitHubClient(GitHubClient):
    """In-memory GitHubClient for tests. Records every mutation; can simulate failures.

    Pass ``fail_on={"open_pull_request"}`` to make that operation raise — used to prove the
    submission service rolls back the reserved WPID when the PR step fails.
    """

    def __init__(
        self,
        *,
        default_branches: dict[str, str] | None = None,
        existing_files: dict[str, str] | None = None,
        fail_on: set[str] | None = None,
    ) -> None:
        # {(repo, branch): sha}
        self.branches: dict[tuple[str, str], str] = {}
        for key, sha in (default_branches or {}).items():
            repo, branch = key.split("#", 1)
            self.branches[(repo, branch)] = sha
        # Files already committed in the repo (visible from any branch cut off the base).
        # Seeded as {"repo#path": blob_sha}.
        self.existing_files: dict[tuple[str, str], str] = {}
        for key, sha in (existing_files or {}).items():
            repo, path = key.split("#", 1)
            self.existing_files[(repo, path)] = sha
        # {(repo, branch, path): (content, message, sha)}
        self.files: dict[tuple[str, str, str], tuple[str, str, str | None]] = {}
        self.pulls: list[PullRequest] = []
        self.merged: set[int] = set()
        self.fail_on = fail_on or set()
        self._next_pr = 1

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_on:
            raise GitHubError(f"simulated failure in {op}")

    def get_branch_sha(self, repo: str, branch: str) -> str:
        self._maybe_fail("get_branch_sha")
        try:
            return self.branches[(repo, branch)]
        except KeyError as exc:
            raise GitHubError(f"no such branch {branch} in {repo}") from exc

    def create_branch(self, repo: str, new_branch: str, from_sha: str) -> None:
        self._maybe_fail("create_branch")
        if (repo, new_branch) in self.branches:
            raise BranchAlreadyExists(f"{new_branch} already exists in {repo}")
        self.branches[(repo, new_branch)] = from_sha

    def get_file_sha(self, repo: str, ref: str, path: str) -> str | None:
        self._maybe_fail("get_file_sha")
        entry = self.files.get((repo, ref, path))
        if entry is not None and entry[2] is not None:
            return entry[2]
        # Not written on this branch yet → fall back to what exists in the repo base.
        return self.existing_files.get((repo, path))

    def put_file(
        self,
        repo: str,
        branch: str,
        path: str,
        content: str,
        message: str,
        *,
        sha: str | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> None:
        self._maybe_fail("put_file")
        # A new blob sha after the write (deterministic, for assertions).
        new_sha = f"sha-{branch}-{path}-{len(content)}"
        self.files[(repo, branch, path)] = (content, message, new_sha)

    def find_open_pr(self, repo: str, head_branch: str) -> PullRequest | None:
        self._maybe_fail("find_open_pr")
        for pr in reversed(self.pulls):
            if pr.head_branch == head_branch:
                return pr
        return None

    def open_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        self._maybe_fail("open_pull_request")
        pr = PullRequest(
            number=self._next_pr,
            html_url=f"https://github.com/{repo}/pull/{self._next_pr}",
            head_branch=head,
        )
        self._next_pr += 1
        self.pulls.append(pr)
        return pr

    def merge_pull_request(self, repo: str, pr_number: int, *, method: str = "squash") -> None:
        self._maybe_fail("merge_pull_request")
        self.merged.add(pr_number)


@dataclass
class HttpGitHubClient(GitHubClient):
    """Real client over the GitHub REST API (httpx). Not exercised by the unit suite.

    ``token`` is the acting identity — a per-user OAuth token so the commit/PR is attributed to
    the submitter (scaffolding-plan §3). Construct one per request with the user's token.
    """

    token: str
    base_url: str = _GITHUB_API
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    def _raise_for(self, resp: httpx.Response, what: str) -> None:
        if resp.is_error:
            raise GitHubError(f"{what} failed: {resp.status_code} {resp.text}")

    def get_branch_sha(self, repo: str, branch: str) -> str:
        resp = self._client.get(f"/repos/{repo}/git/ref/heads/{branch}")
        self._raise_for(resp, f"get_branch_sha({branch})")
        return resp.json()["object"]["sha"]

    def create_branch(self, repo: str, new_branch: str, from_sha: str) -> None:
        resp = self._client.post(
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{new_branch}", "sha": from_sha},
        )
        if resp.status_code == 422:
            raise BranchAlreadyExists(f"{new_branch} already exists in {repo}")
        self._raise_for(resp, f"create_branch({new_branch})")

    def get_file_sha(self, repo: str, ref: str, path: str) -> str | None:
        resp = self._client.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
        if resp.status_code == 404:
            return None
        self._raise_for(resp, f"get_file_sha({path})")
        return resp.json()["sha"]

    def put_file(
        self,
        repo: str,
        branch: str,
        path: str,
        content: str,
        message: str,
        *,
        sha: str | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha is not None:
            payload["sha"] = sha  # required by GitHub to update an existing file
        if author_name and author_email:
            payload["author"] = {"name": author_name, "email": author_email}
        resp = self._client.put(f"/repos/{repo}/contents/{path}", json=payload)
        self._raise_for(resp, f"put_file({path})")

    def find_open_pr(self, repo: str, head_branch: str) -> PullRequest | None:
        owner = repo.split("/", 1)[0]
        resp = self._client.get(
            f"/repos/{repo}/pulls",
            params={"state": "open", "head": f"{owner}:{head_branch}"},
        )
        self._raise_for(resp, "find_open_pr")
        items = resp.json()
        if not items:
            return None
        data = items[0]
        return PullRequest(
            number=data["number"], html_url=data["html_url"], head_branch=head_branch
        )

    def open_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        resp = self._client.post(
            f"/repos/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        self._raise_for(resp, "open_pull_request")
        data = resp.json()
        return PullRequest(number=data["number"], html_url=data["html_url"], head_branch=head)

    def merge_pull_request(self, repo: str, pr_number: int, *, method: str = "squash") -> None:
        resp = self._client.put(
            f"/repos/{repo}/pulls/{pr_number}/merge", json={"merge_method": method}
        )
        self._raise_for(resp, f"merge_pull_request({pr_number})")
