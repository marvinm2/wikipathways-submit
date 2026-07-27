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
    def get_file_content(self, repo: str, ref: str, path: str) -> bytes | None:
        """Return the raw bytes of ``path`` at ``ref`` (branch/sha), or None if absent."""

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
    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        """Post a new comment on an issue/PR (not upserted) — e.g. a curator's change request."""

    @abstractmethod
    def request_pr_reviewer(self, repo: str, pr_number: int, reviewer: str) -> None:
        """Request ``reviewer`` as a reviewer on the PR (mirrors an app-side assignment).

        GitHub refuses to request a review from the PR author or a non-collaborator, so callers
        treat this as best-effort."""

    @abstractmethod
    def get_pull_request_state(self, repo: str, pr_number: int) -> str | None:
        """State of a PR: ``"open"``, ``"closed"``, ``"merged"``, or None if it does not exist.

        Used to reconcile review rows against PRs closed/merged outside the app (issue #1)."""

    @abstractmethod
    def merge_pull_request(self, repo: str, pr_number: int, *, method: str = "squash") -> None:
        """Merge a PR. Raises GitHubError if the merge is not allowed."""

    @abstractmethod
    def upsert_issue_comment(
        self, repo: str, issue_number: int, body: str, *, marker: str
    ) -> None:
        """Post ``body`` as an issue/PR comment, or update the existing one carrying ``marker``.

        ``marker`` is a hidden token (an HTML comment) embedded in the body so the app keeps a
        single read-only *mirror* comment in sync instead of spamming the PR on every update.
        A privileged (bot) operation — the mirror must not be attributed to the submitter.
        """

    @abstractmethod
    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        """Return the logins of the members of ``org/team_slug`` (curator whitelist, issue #9)."""

    @abstractmethod
    def pr_preview_status(
        self, repo: str, pr_number: int, *, workflow_file: str, artifact_name: str
    ) -> str:
        """State of the PR-preview render for ``pr_number`` (issue #11), *without* downloading it.

        One of: ``"pending"`` (workflow not run / still running), ``"ready"`` (completed
        successfully with the preview artifact present), ``"failed"`` (run failed / artifact
        missing), ``"absent"`` (no such PR). Cheap: no artifact bytes are transferred.
        """

    @abstractmethod
    def download_pr_preview_zip(
        self, repo: str, pr_number: int, *, workflow_file: str, artifact_name: str
    ) -> bytes | None:
        """Download the PR-preview artifact zip for ``pr_number``, or None if not available."""


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
        existing_contents: dict[str, str] | None = None,
        fail_on: set[str] | None = None,
        team_members: dict[str, list[str]] | None = None,
        previews: dict[int, dict] | None = None,
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
        # Base-branch file *contents* (for get_file_content), seeded as {"repo#path": content}.
        self.existing_contents: dict[tuple[str, str], str] = {}
        for key, content in (existing_contents or {}).items():
            repo, path = key.split("#", 1)
            self.existing_contents[(repo, path)] = content
        # {(repo, branch, path): (content, message, sha)}
        self.files: dict[tuple[str, str, str], tuple[str, str, str | None]] = {}
        self.pulls: list[PullRequest] = []
        # {pr_number: {"title", "body", "base", "head"}} — the human-facing PR fields the API
        # sends. Captured so tests can assert on the title/body a reviewer actually sees.
        self.pull_meta: dict[int, dict[str, str]] = {}
        self.merged: set[int] = set()
        # PRs closed without merging (tests set this to simulate an out-of-band close).
        self.closed: set[int] = set()
        # {pr_number: [reviewer, ...]} — reviewers requested via request_pr_reviewer.
        self.review_requests: dict[int, list[str]] = {}
        # {(repo, issue_number): [body, ...]} — plain comments posted via create_issue_comment.
        self.issue_comments: dict[tuple[str, int], list[str]] = {}
        # {(repo, issue_number): {marker: body}} — one comment per marker (upsert semantics).
        self.comments: dict[tuple[str, int], dict[str, str]] = {}
        # {"org/team-slug": [login, ...]}
        self.team_members = dict(team_members or {})
        # {pr_number: {"status": str, "zip": bytes | None}} — PR-preview artifact (issue #11).
        self.previews = dict(previews or {})
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

    def get_file_content(self, repo: str, ref: str, path: str) -> bytes | None:
        self._maybe_fail("get_file_content")
        entry = self.files.get((repo, ref, path))
        if entry is not None:
            return entry[0].encode("utf-8")
        content = self.existing_contents.get((repo, path))
        return content.encode("utf-8") if content is not None else None

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
        self.pull_meta[pr.number] = {"title": title, "body": body, "base": base, "head": head}
        self._next_pr += 1
        self.pulls.append(pr)
        return pr

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        self._maybe_fail("create_issue_comment")
        self.issue_comments.setdefault((repo, issue_number), []).append(body)

    def request_pr_reviewer(self, repo: str, pr_number: int, reviewer: str) -> None:
        self._maybe_fail("request_pr_reviewer")
        self.review_requests.setdefault(pr_number, []).append(reviewer)

    def get_pull_request_state(self, repo: str, pr_number: int) -> str | None:
        self._maybe_fail("get_pull_request_state")
        if pr_number in self.merged:
            return "merged"
        if pr_number in self.closed:
            return "closed"
        if any(pr.number == pr_number for pr in self.pulls):
            return "open"
        return None

    def merge_pull_request(self, repo: str, pr_number: int, *, method: str = "squash") -> None:
        self._maybe_fail("merge_pull_request")
        self.merged.add(pr_number)

    def upsert_issue_comment(
        self, repo: str, issue_number: int, body: str, *, marker: str
    ) -> None:
        self._maybe_fail("upsert_issue_comment")
        self.comments.setdefault((repo, issue_number), {})[marker] = body

    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        self._maybe_fail("list_team_members")
        return list(self.team_members.get(f"{org}/{team_slug}", []))

    def pr_preview_status(
        self, repo: str, pr_number: int, *, workflow_file: str, artifact_name: str
    ) -> str:
        self._maybe_fail("pr_preview_status")
        return self.previews.get(pr_number, {}).get("status", "absent")

    def download_pr_preview_zip(
        self, repo: str, pr_number: int, *, workflow_file: str, artifact_name: str
    ) -> bytes | None:
        self._maybe_fail("download_pr_preview_zip")
        return self.previews.get(pr_number, {}).get("zip")


@dataclass
class HttpGitHubClient(GitHubClient):
    """Real client over the GitHub REST API (httpx). Not exercised by the unit suite.

    ``token`` is the acting identity — a per-user OAuth token so the commit/PR is attributed to
    the submitter (scaffolding-plan §3). Construct one per request with the user's token.
    """

    token: str
    base_url: str = _GITHUB_API
    transport: httpx.BaseTransport | None = None  # injection seam for tests
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            base_url=self.base_url,
            transport=self.transport,
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

    def get_file_content(self, repo: str, ref: str, path: str) -> bytes | None:
        # The contents API inlines base64 for blobs up to 1 MB (GPML files are ~tens of KB).
        resp = self._client.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
        if resp.status_code == 404:
            return None
        self._raise_for(resp, f"get_file_content({path})")
        body = resp.json()
        if body.get("encoding") != "base64" or "content" not in body:
            return None
        return base64.b64decode(body["content"])

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

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        resp = self._client.post(
            f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body}
        )
        self._raise_for(resp, f"create_issue_comment({issue_number})")

    def request_pr_reviewer(self, repo: str, pr_number: int, reviewer: str) -> None:
        resp = self._client.post(
            f"/repos/{repo}/pulls/{pr_number}/requested_reviewers",
            json={"reviewers": [reviewer]},
        )
        # 422 = GitHub declined (author can't review, or not a collaborator). Surface as a
        # GitHubError so the caller can swallow it without failing the app-side assignment.
        self._raise_for(resp, f"request_pr_reviewer({reviewer})")

    def get_pull_request_state(self, repo: str, pr_number: int) -> str | None:
        resp = self._client.get(f"/repos/{repo}/pulls/{pr_number}")
        if resp.status_code == 404:
            return None
        self._raise_for(resp, f"get_pull_request_state({pr_number})")
        data = resp.json()
        if data.get("merged"):
            return "merged"
        return data.get("state")  # "open" | "closed"

    def merge_pull_request(self, repo: str, pr_number: int, *, method: str = "squash") -> None:
        resp = self._client.put(
            f"/repos/{repo}/pulls/{pr_number}/merge", json={"merge_method": method}
        )
        self._raise_for(resp, f"merge_pull_request({pr_number})")

    def upsert_issue_comment(
        self, repo: str, issue_number: int, body: str, *, marker: str
    ) -> None:
        # Find our existing mirror comment (first page is plenty — the PR won't have hundreds).
        resp = self._client.get(
            f"/repos/{repo}/issues/{issue_number}/comments", params={"per_page": 100}
        )
        self._raise_for(resp, f"list_comments({issue_number})")
        existing_id: int | None = None
        for comment in resp.json():
            if marker in (comment.get("body") or ""):
                existing_id = comment["id"]
                break
        if existing_id is not None:
            resp = self._client.patch(
                f"/repos/{repo}/issues/comments/{existing_id}", json={"body": body}
            )
            self._raise_for(resp, f"update_comment({existing_id})")
        else:
            resp = self._client.post(
                f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body}
            )
            self._raise_for(resp, f"create_comment({issue_number})")

    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        members: list[str] = []
        page = 1
        while True:
            resp = self._client.get(
                f"/orgs/{org}/teams/{team_slug}/members",
                params={"per_page": 100, "page": page},
            )
            self._raise_for(resp, f"list_team_members({org}/{team_slug})")
            batch = resp.json()
            if not batch:
                break
            members.extend(m["login"] for m in batch)
            if len(batch) < 100:
                break
            page += 1
        return members

    def _latest_preview_run(self, repo: str, pr_number: int, workflow_file: str):
        """Return (status, run_id) for the newest preview run on the PR's head SHA.

        status ∈ pending|success|failed|absent; run_id is None unless the run completed OK.
        """
        pr = self._client.get(f"/repos/{repo}/pulls/{pr_number}")
        if pr.status_code == 404:
            return "absent", None
        self._raise_for(pr, f"get_pull({pr_number})")
        head_sha = pr.json()["head"]["sha"]
        runs = self._client.get(
            f"/repos/{repo}/actions/workflows/{workflow_file}/runs",
            params={"head_sha": head_sha, "per_page": 1},
        )
        self._raise_for(runs, "list_workflow_runs")
        items = runs.json().get("workflow_runs", [])
        if not items:
            return "pending", None
        run = items[0]
        if run.get("status") != "completed":
            return "pending", None
        if run.get("conclusion") != "success":
            return "failed", None
        return "success", run["id"]

    def _preview_artifact_id(self, repo: str, run_id: int, artifact_name: str) -> int | None:
        resp = self._client.get(f"/repos/{repo}/actions/runs/{run_id}/artifacts")
        self._raise_for(resp, f"list_run_artifacts({run_id})")
        for art in resp.json().get("artifacts", []):
            if art.get("name") == artifact_name and not art.get("expired"):
                return art["id"]
        return None

    def pr_preview_status(
        self, repo: str, pr_number: int, *, workflow_file: str, artifact_name: str
    ) -> str:
        status, run_id = self._latest_preview_run(repo, pr_number, workflow_file)
        if status != "success":
            return {"absent": "absent", "pending": "pending"}.get(status, "failed")
        return "ready" if self._preview_artifact_id(repo, run_id, artifact_name) else "failed"

    def download_pr_preview_zip(
        self, repo: str, pr_number: int, *, workflow_file: str, artifact_name: str
    ) -> bytes | None:
        status, run_id = self._latest_preview_run(repo, pr_number, workflow_file)
        if status != "success":
            return None
        artifact_id = self._preview_artifact_id(repo, run_id, artifact_name)
        if artifact_id is None:
            return None
        resp = self._client.get(
            f"/repos/{repo}/actions/artifacts/{artifact_id}/zip", follow_redirects=True
        )
        self._raise_for(resp, f"download_artifact({artifact_id})")
        return resp.content
