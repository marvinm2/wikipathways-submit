"""Real ``FloorProvider`` backed by GitHub: max WPID over the repo tree ∪ open PRs ∪ app branches.

This is the production floor for :class:`app.wpid.allocator.WpidAllocator`. It is intentionally
kept separate from the allocator (which is pure and unit-tested without a network).

Union rationale (design §4.2): reading only the merged tree is the historical bug. An id
reserved in an open PR (e.g. WP5641) is not yet in the tree, so we must also scan open PRs.
Branches named ``submit/WP<id>`` / ``update/WP<id>`` are counted too: a PR that was closed
unmerged leaves its branch behind, and the submission flow creates that exact branch name, so an
id whose branch still exists is not reusable even though no tree entry and no open PR claim it.
Local live reservations are added by the allocator itself on top of this floor.
"""
from __future__ import annotations

import re

import httpx

_PATHWAY_DIR_RE = re.compile(r"(?:^|/)pathways/WP(\d+)(?:/|$)")
_WP_NAME_RE = re.compile(r"^WP(\d+)$")
#: Branches this app creates. Their names encode a claimed WPID even after the PR is gone.
_APP_BRANCH_RE = re.compile(r"^(?:submit|update)/WP(\d+)$")

_GITHUB_API = "https://api.github.com"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _tree_max(client: httpx.Client, owner: str, repo: str, branch: str) -> int:
    """Max WPID among ``pathways/WP*`` directory names on ``branch``.

    Uses the git-tree API (not the contents API, which truncates directory listings at 1000
    entries — there are ~2085 pathway dirs).
    """
    root = client.get(f"/repos/{owner}/{repo}/git/trees/{branch}").json()
    pathways_sha = next(
        (e["sha"] for e in root.get("tree", []) if e["path"] == "pathways" and e["type"] == "tree"),
        None,
    )
    if pathways_sha is None:
        return 0
    tree = client.get(f"/repos/{owner}/{repo}/git/trees/{pathways_sha}").json()
    best = 0
    for entry in tree.get("tree", []):
        m = _WP_NAME_RE.match(entry.get("path", ""))
        if m:
            best = max(best, int(m.group(1)))
    return best


def _open_pr_max(client: httpx.Client, owner: str, repo: str) -> int:
    """Max WPID touched by any open PR (paths like ``pathways/WP1234/...``)."""
    best = 0
    page = 1
    while True:
        prs = client.get(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "open", "per_page": 100, "page": page},
        ).json()
        if not prs:
            break
        for pr in prs:
            fpage = 1
            while True:
                files = client.get(
                    f"/repos/{owner}/{repo}/pulls/{pr['number']}/files",
                    params={"per_page": 100, "page": fpage},
                ).json()
                if not files:
                    break
                for f in files:
                    m = _PATHWAY_DIR_RE.search(f.get("filename", ""))
                    if m:
                        best = max(best, int(m.group(1)))
                if len(files) < 100:
                    break
                fpage += 1
        if len(prs) < 100:
            break
        page += 1
    return best


def _branch_max(client: httpx.Client, owner: str, repo: str) -> int:
    """Max WPID among leftover ``submit/WP<id>`` / ``update/WP<id>`` branches.

    A closed-unmerged PR leaves its branch behind; re-allocating that id would make
    ``create_branch`` fail with ``BranchAlreadyExists``, so the id is treated as taken.
    """
    best = 0
    page = 1
    while True:
        branches = client.get(
            f"/repos/{owner}/{repo}/branches", params={"per_page": 100, "page": page}
        ).json()
        if not branches:
            break
        for branch in branches:
            m = _APP_BRANCH_RE.match(branch.get("name", ""))
            if m:
                best = max(best, int(m.group(1)))
        if len(branches) < 100:
            break
        page += 1
    return best


def github_wpid_floor(
    owner: str,
    repo: str,
    token: str,
    *,
    branch: str = "main",
    client: httpx.Client | None = None,
) -> int:
    """Return ``max(WPID)`` over the merged tree ∪ open PRs ∪ app branches. 0 if nothing found."""
    owned_client = client is None
    client = client or httpx.Client(base_url=_GITHUB_API, headers=_headers(token), timeout=30.0)
    try:
        return max(
            _tree_max(client, owner, repo, branch),
            _open_pr_max(client, owner, repo),
            _branch_max(client, owner, repo),
        )
    finally:
        if owned_client:
            client.close()
