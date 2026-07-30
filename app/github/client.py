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


@dataclass(frozen=True)
class PullRequestDetail:
    """Everything the publication handshake needs from one PR read.

    Deliberately a single fetch: reconciling a review against a target repo that publishes via
    its own Actions needs the state, the merge flag, the labels and the body together, and doing
    that as four calls would multiply the dashboard's request count by the size of the queue.
    """

    number: int
    html_url: str
    head_branch: str
    head_sha: str
    state: str  # "open" | "closed"
    merged: bool
    title: str
    body: str
    labels: list[str]
    author: str


@dataclass(frozen=True)
class WorkflowRun:
    id: int
    html_url: str
    status: str  # "queued" | "in_progress" | "completed"
    conclusion: str | None  # "success" | "failure" | ... | None while running
    created_at: str


def _workflow_run(data: dict) -> WorkflowRun:
    return WorkflowRun(
        id=data["id"],
        html_url=data.get("html_url", ""),
        status=data.get("status", ""),
        conclusion=data.get("conclusion"),
        created_at=data.get("created_at", ""),
    )


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
    def delete_file(self, repo: str, branch: str, path: str, message: str, *, sha: str) -> None:
        """Remove ``path`` from ``branch``. ``sha`` is the blob SHA being deleted.

        Exists for exactly one caller: taking the submission placeholder back off the base
        branch after somebody merged a pipeline pull request. Nothing else in the app removes
        content from the content repository.
        """

    @abstractmethod
    def find_open_pr(self, repo: str, head_branch: str) -> PullRequest | None:
        """Return the open PR whose head is ``head_branch``, or None."""

    @abstractmethod
    def find_open_pr_touching(
        self, repo: str, path_prefix: str, *, limit: int = 40
    ) -> int | None:
        """The number of an open PR that changes a file under ``path_prefix``, or None.

        This is what makes the pathway check-out lock more than an app-internal flag: a power
        user can open a raw pull request against the content repo without going near this app,
        and starting a second edit of the same GPML on top of that is the unmergeable-divergence
        failure the lock exists to prevent (design §4.3).

        There is no GitHub query for "open PRs touching this path", so this walks the open pull
        requests and reads each one's file list, newest first, stopping at ``limit``. That makes
        it too expensive for a page render; it belongs on the update write path, which happens a
        few times a day.
        """

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

    # --- Labels -------------------------------------------------------------------------
    # On a target repo that publishes through its own Actions, a label is not decoration: its
    # label dispatcher turns `accepted`/`rejected` into workflow runs. Applying one is the
    # approval, so these are write operations with real consequences.

    @abstractmethod
    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        """Add labels to an issue/PR, leaving existing ones in place."""

    @abstractmethod
    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        """Remove one label. A label that isn't there is not an error."""

    @abstractmethod
    def list_labels(self, repo: str, issue_number: int) -> list[str]:
        """Return the label names currently on an issue/PR."""

    # --- Pull-request reads -------------------------------------------------------------

    @abstractmethod
    def get_pull_request(self, repo: str, pr_number: int) -> PullRequestDetail | None:
        """Full PR state in one call, or None if it does not exist."""

    @abstractmethod
    def list_issue_comments(self, repo: str, issue_number: int) -> list[str]:
        """Return the bodies of an issue/PR's comments, oldest first.

        The target repo's publish workflow announces the assigned WPID in a comment, which
        survives the PR-description rewrites its own pipeline performs.
        """

    @abstractmethod
    def close_pull_request(self, repo: str, pr_number: int) -> None:
        """Close a PR without merging."""

    # --- Actions (read-only) ------------------------------------------------------------

    @abstractmethod
    def latest_workflow_run_for_pr(
        self, repo: str, pr_number: int, *, workflow_file: str
    ) -> WorkflowRun | None:
        """Newest run of ``workflow_file`` against the PR's head SHA, or None if there is none."""

    @abstractmethod
    def recent_workflow_runs(
        self, repo: str, workflow_file: str, *, limit: int = 5
    ) -> list[WorkflowRun]:
        """Newest runs of ``workflow_file``, whatever triggered them.

        For display only. A ``workflow_dispatch`` run carries no head SHA or PR reference, so
        there is no reliable way to join it to a review — never drive state from this.
        """


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
        # {pr_number: {"status": str}} — PR-preview CI state, the merge gate (issue #11).
        self.previews = dict(previews or {})
        # {(repo, issue_number): {label, ...}}
        self.labels: dict[tuple[str, int], set[str]] = {}
        # [(repo, issue_number, "add"|"remove", label)] — ordered, so a test can assert that the
        # reason comment was posted *before* the label that triggers the repo's workflow.
        self.label_log: list[tuple[str, int, str, str]] = []
        # {(repo, workflow_file): [WorkflowRun, ...]}, newest last.
        self.workflow_runs: dict[tuple[str, str], list[WorkflowRun]] = {}
        # [(repo, branch, path, message)] — every delete_file, so a test can assert the repair
        # touched exactly the placeholder and nothing else.
        self.deleted: list[tuple[str, str, str, str]] = []
        self.fail_on = fail_on or set()
        self._next_pr = 1
        self._next_run = 1000

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
        # GitHub answers 422 ("sha" wasn't supplied) when the contents API is asked to *create*
        # a file that is already there. Modelling that here is what stops a caller from
        # assuming a path is free: the placeholder path is shared by every new submission, so
        # one stray copy of it on the base branch would otherwise break the front door.
        if sha is None and self.get_file_sha(repo, branch, path) is not None:
            raise GitHubError(f'put_file({path}) failed: 422 "sha" wasn\'t supplied')
        # A new blob sha after the write (deterministic, for assertions).
        new_sha = f"sha-{branch}-{path}-{len(content)}"
        self.files[(repo, branch, path)] = (content, message, new_sha)

    def delete_file(self, repo: str, branch: str, path: str, message: str, *, sha: str) -> None:
        """Drops the file from both the branch map and the base — the app only ever deletes on
        the base branch, so modelling a branch-local tombstone would be fiction nothing uses."""
        self._maybe_fail("delete_file")
        if self.get_file_sha(repo, branch, path) is None:
            raise GitHubError(f"delete_file({path}) failed: 404 not found")
        self.files.pop((repo, branch, path), None)
        self.existing_files.pop((repo, path), None)
        self.existing_contents.pop((repo, path), None)
        self.deleted.append((repo, branch, path, message))

    def find_open_pr(self, repo: str, head_branch: str) -> PullRequest | None:
        self._maybe_fail("find_open_pr")
        for pr in reversed(self.pulls):
            # "open" is part of the contract: the real client filters on state=open. Without
            # this a revise would happily commit onto a branch whose PR the target repo's
            # publish workflow already closed.
            if pr.head_branch == head_branch and pr.number not in self.closed | self.merged:
                return pr
        return None

    def find_open_pr_touching(
        self, repo: str, path_prefix: str, *, limit: int = 40
    ) -> int | None:
        self._maybe_fail("find_open_pr_touching")
        for pr in reversed(self.pulls[-limit:]):
            if pr.number in self.closed | self.merged:
                continue
            for (r, branch, path) in self.files:
                if r == repo and branch == pr.head_branch and path.startswith(path_prefix):
                    return pr.number
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

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        self._maybe_fail("add_labels")
        current = self.labels.setdefault((repo, issue_number), set())
        for label in labels:
            current.add(label)
            self.label_log.append((repo, issue_number, "add", label))

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        self._maybe_fail("remove_label")
        self.labels.setdefault((repo, issue_number), set()).discard(label)
        self.label_log.append((repo, issue_number, "remove", label))

    def list_labels(self, repo: str, issue_number: int) -> list[str]:
        self._maybe_fail("list_labels")
        return sorted(self.labels.get((repo, issue_number), set()))

    def get_pull_request(self, repo: str, pr_number: int) -> PullRequestDetail | None:
        self._maybe_fail("get_pull_request")
        pr = next((p for p in self.pulls if p.number == pr_number), None)
        if pr is None:
            return None
        meta = self.pull_meta.get(pr_number, {})
        merged = pr_number in self.merged
        return PullRequestDetail(
            number=pr.number,
            html_url=pr.html_url,
            head_branch=pr.head_branch,
            head_sha=self.branches.get((repo, pr.head_branch), f"sha-{pr.head_branch}"),
            state="closed" if merged or pr_number in self.closed else "open",
            merged=merged,
            title=meta.get("title", ""),
            body=meta.get("body", ""),
            labels=sorted(self.labels.get((repo, pr_number), set())),
            author=meta.get("author", ""),
        )

    def list_issue_comments(self, repo: str, issue_number: int) -> list[str]:
        self._maybe_fail("list_issue_comments")
        plain = self.issue_comments.get((repo, issue_number), [])
        upserted = list(self.comments.get((repo, issue_number), {}).values())
        return [*plain, *upserted]

    def close_pull_request(self, repo: str, pr_number: int) -> None:
        self._maybe_fail("close_pull_request")
        self.closed.add(pr_number)

    def latest_workflow_run_for_pr(
        self, repo: str, pr_number: int, *, workflow_file: str
    ) -> WorkflowRun | None:
        self._maybe_fail("latest_workflow_run_for_pr")
        runs = self.workflow_runs.get((repo, workflow_file), [])
        return runs[-1] if runs else None

    def recent_workflow_runs(
        self, repo: str, workflow_file: str, *, limit: int = 5
    ) -> list[WorkflowRun]:
        self._maybe_fail("recent_workflow_runs")
        runs = self.workflow_runs.get((repo, workflow_file), [])
        return list(reversed(runs[-limit:]))

    # --- Simulating the target repo's own pipeline --------------------------------------
    # Modelled on wikipathways/sandbox-wp-db (docs/sandbox-pipeline.md). These exist because the
    # whole integration is a handshake with workflows we do not run: label goes on, PR gets
    # closed *unmerged*, and the assigned WPID comes back in a comment. Without a way to replay
    # that sequence there is nothing to test.

    def record_workflow_run(
        self, repo: str, workflow_file: str, *, conclusion: str | None = "success"
    ) -> WorkflowRun:
        run = WorkflowRun(
            id=self._next_run,
            html_url=f"https://github.com/{repo}/actions/runs/{self._next_run}",
            status="completed" if conclusion else "in_progress",
            conclusion=conclusion,
            created_at="2026-07-27T12:00:00Z",
        )
        self._next_run += 1
        self.workflow_runs.setdefault((repo, workflow_file), []).append(run)
        return run

    def simulate_workflow1(
        self,
        repo: str,
        pr_number: int,
        *,
        ok: bool = True,
        workflow_file: str = "1_on_pull_request.yml",
    ) -> None:
        """The target repo's PR processor: it rewrites the PR body wholesale, twice.

        The body clobbering is the point — it is why the app's durable record is a comment and
        not the description.
        """
        meta = self.pull_meta.setdefault(pr_number, {})
        meta["body"] = (
            f"## Pathway Information\n\n**WPID**: WP0__PR{pr_number}\n\n---\n"
            if ok
            else "\n## Pathway Information\n\nProcessing...\n"
        )
        self.record_workflow_run(repo, workflow_file, conclusion="success" if ok else "failure")

    def simulate_3a(
        self,
        repo: str,
        pr_number: int,
        *,
        wpid: int | None,
        marker: str = "<!-- wikipathways-publish ",
    ) -> None:
        """The target repo's publish workflow: announce the WPID, then close **unmerged**.

        ``wpid=None`` replays the failure this repo has actually shown — the PR gets closed with
        no announcement, so the app has to notice rather than assume success.
        """
        if wpid is not None:
            self.create_issue_comment(
                repo,
                pr_number,
                f'{marker}{{"pr":{pr_number},"wpid":{wpid},"status":"published"}} -->\n'
                f"Published as WP{wpid}.",
            )
        self.closed.add(pr_number)

    def simulate_3b(self, repo: str, pr_number: int) -> None:
        """The target repo's rejection workflow: comment, then close unmerged."""
        self.create_issue_comment(repo, pr_number, "This pull request has been rejected.")
        self.closed.add(pr_number)

    def simulate_dispatcher_failure(self, repo: str, pr_number: int) -> None:
        """The label goes on and nothing happens. Historically the most likely outcome."""
        return None


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

    def delete_file(self, repo: str, branch: str, path: str, message: str, *, sha: str) -> None:
        resp = self._client.request(
            "DELETE",
            f"/repos/{repo}/contents/{path}",
            # DELETE with a body: the contents API takes the sha and branch that way, and httpx
            # needs the explicit request() call because .delete() has no json= parameter.
            json={"message": message, "sha": sha, "branch": branch},
        )
        self._raise_for(resp, f"delete_file({path})")

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

    def find_open_pr_touching(
        self, repo: str, path_prefix: str, *, limit: int = 40
    ) -> int | None:
        resp = self._client.get(
            f"/repos/{repo}/pulls",
            params={"state": "open", "per_page": min(limit, 100), "sort": "long-running",
                    "direction": "desc"},
        )
        self._raise_for(resp, "find_open_pr_touching")
        pulls = resp.json()[:limit]
        # A pull request whose head branch is one of ours is one of ours. Skipping them first
        # keeps a submitter's own in-flight edit from reading as a foreign writer, and saves a
        # file listing per skipped pull request.
        wpid = path_prefix.rstrip("/").rsplit("/", 1)[-1]
        ours = (f"update/{wpid}", f"submit/{wpid}")
        for pull in pulls:
            head = str((pull.get("head") or {}).get("ref", ""))
            if head.startswith(ours):
                continue
            files = self._client.get(
                f"/repos/{repo}/pulls/{pull['number']}/files", params={"per_page": 100}
            )
            # One unreadable pull request must not be read as "nothing touches this pathway",
            # but neither should it abort the scan — keep looking through the rest.
            if files.is_error:
                continue
            for entry in files.json():
                if str(entry.get("filename", "")).startswith(path_prefix):
                    return int(pull["number"])
        return None

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

    def get_pull_request(self, repo: str, pr_number: int) -> PullRequestDetail | None:
        resp = self._client.get(f"/repos/{repo}/pulls/{pr_number}")
        if resp.status_code == 404:
            return None
        self._raise_for(resp, f"get_pull_request({pr_number})")
        data = resp.json()
        return PullRequestDetail(
            number=data["number"],
            html_url=data.get("html_url", ""),
            head_branch=(data.get("head") or {}).get("ref", ""),
            head_sha=(data.get("head") or {}).get("sha", ""),
            state=data.get("state", ""),
            merged=bool(data.get("merged")),
            title=data.get("title") or "",
            body=data.get("body") or "",
            labels=[label["name"] for label in data.get("labels") or []],
            author=(data.get("user") or {}).get("login", ""),
        )

    def get_pull_request_state(self, repo: str, pr_number: int) -> str | None:
        detail = self.get_pull_request(repo, pr_number)
        if detail is None:
            return None
        return "merged" if detail.merged else detail.state  # "open" | "closed"

    def list_issue_comments(self, repo: str, issue_number: int) -> list[str]:
        bodies: list[str] = []
        page = 1
        while True:
            resp = self._client.get(
                f"/repos/{repo}/issues/{issue_number}/comments",
                params={"per_page": 100, "page": page},
            )
            self._raise_for(resp, f"list_issue_comments({issue_number})")
            batch = resp.json()
            bodies.extend(c.get("body") or "" for c in batch)
            if len(batch) < 100:
                return bodies
            page += 1

    def close_pull_request(self, repo: str, pr_number: int) -> None:
        resp = self._client.patch(f"/repos/{repo}/pulls/{pr_number}", json={"state": "closed"})
        self._raise_for(resp, f"close_pull_request({pr_number})")

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        # Labels live on the Issues API even for a PR, so this needs Issues:write on the App.
        resp = self._client.post(
            f"/repos/{repo}/issues/{issue_number}/labels", json={"labels": labels}
        )
        self._raise_for(resp, f"add_labels({labels})")

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        resp = self._client.delete(f"/repos/{repo}/issues/{issue_number}/labels/{label}")
        if resp.status_code == 404:
            return  # not on the PR — the caller's intent is already satisfied
        self._raise_for(resp, f"remove_label({label})")

    def list_labels(self, repo: str, issue_number: int) -> list[str]:
        resp = self._client.get(
            f"/repos/{repo}/issues/{issue_number}/labels", params={"per_page": 100}
        )
        self._raise_for(resp, f"list_labels({issue_number})")
        return [label["name"] for label in resp.json()]

    def latest_workflow_run_for_pr(
        self, repo: str, pr_number: int, *, workflow_file: str
    ) -> WorkflowRun | None:
        detail = self.get_pull_request(repo, pr_number)
        if detail is None:
            return None
        resp = self._client.get(
            f"/repos/{repo}/actions/workflows/{workflow_file}/runs",
            params={"head_sha": detail.head_sha, "per_page": 1},
        )
        self._raise_for(resp, "list_workflow_runs")
        items = resp.json().get("workflow_runs", [])
        return _workflow_run(items[0]) if items else None

    def recent_workflow_runs(
        self, repo: str, workflow_file: str, *, limit: int = 5
    ) -> list[WorkflowRun]:
        resp = self._client.get(
            f"/repos/{repo}/actions/workflows/{workflow_file}/runs",
            params={"per_page": limit},
        )
        self._raise_for(resp, "recent_workflow_runs")
        return [_workflow_run(run) for run in resp.json().get("workflow_runs", [])]

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
        if self.get_pull_request(repo, pr_number) is None:
            return "absent", None
        run = self.latest_workflow_run_for_pr(repo, pr_number, workflow_file=workflow_file)
        if run is None or run.status != "completed":
            return "pending", None
        if run.conclusion != "success":
            return "failed", None
        return "success", run.id

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
