"""The GitHub-backed WPID floor: tree ∪ open PRs ∪ leftover app branches.

Driven through an injected ``httpx.MockTransport`` so the union logic is covered without a token
or a network (same approach as the OAuth / GitHub App tests).
"""
from __future__ import annotations

import httpx
import pytest

from app.wpid.github_floor import github_wpid_floor

OWNER, REPO = "wikipathways", "wikipathways-database"


def _client(routes: dict[str, object]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        if body is None:
            raise AssertionError(f"unexpected request: {request.url}")
        if callable(body):
            body = body(request)
        return httpx.Response(200, json=body)

    return httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )


def _tree_routes(max_wpid: int) -> dict[str, object]:
    return {
        f"/repos/{OWNER}/{REPO}/git/trees/main": {
            "tree": [{"path": "pathways", "type": "tree", "sha": "treesha"}]
        },
        f"/repos/{OWNER}/{REPO}/git/trees/treesha": {
            "tree": [{"path": f"WP{n}"} for n in range(max_wpid - 2, max_wpid + 1)]
        },
    }


def _page(items: list[dict], request: httpx.Request) -> list[dict]:
    """Serve `items` on page 1 and nothing after, so the paginating loops terminate."""
    return items if request.url.params.get("page") in (None, "1") else []


@pytest.mark.parametrize(
    "tree_max, open_pr_wpid, branch_names, expected",
    [
        # Tree alone.
        (5640, None, [], 5640),
        # An id claimed only by an open PR outstrips the merged tree (the historical bug).
        (5640, 5643, [], 5643),
        # An id claimed only by a leftover branch — PR closed unmerged, branch never deleted.
        # Nothing in the tree or the open PRs knows about it, but submit/WP5644 cannot be
        # created twice, so the floor must count it.
        (5640, 5643, ["submit/WP5644"], 5644),
        # update/ branches count the same way.
        (5640, None, ["update/WP5650"], 5650),
        # Branches that are not app-created do not: they encode no WPID claim.
        (5640, None, ["update-wp5999-mitophagy", "wp6000/fixup", "main"], 5640),
    ],
)
def test_floor_is_the_union(tree_max, open_pr_wpid, branch_names, expected):
    pulls = [{"number": 7}] if open_pr_wpid else []
    files = [{"filename": f"pathways/WP{open_pr_wpid}/WP{open_pr_wpid}.gpml"}]
    routes = {
        **_tree_routes(tree_max),
        f"/repos/{OWNER}/{REPO}/pulls": lambda r: _page(pulls, r),
        f"/repos/{OWNER}/{REPO}/pulls/7/files": lambda r: _page(files, r),
        f"/repos/{OWNER}/{REPO}/branches": lambda r: _page(
            [{"name": n} for n in branch_names], r
        ),
    }
    with _client(routes) as client:
        assert github_wpid_floor(OWNER, REPO, "tok", client=client) == expected
