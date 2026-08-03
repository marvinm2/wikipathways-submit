"""Fork-per-submitter: the branch lives on the submitter's fork, the pull request is theirs.

Issue #22. The thing under test is not "does a fork get made" but the four places where a
cross-repository submission differs from a same-repository one, each of which is a way to get it
subtly wrong:

- the base commit is read from the **content repo**, never from the fork, which can be a year
  stale — cutting from the fork's default branch would silently revert everything merged upstream;
- the pull request's ``head`` is ``owner:branch``, not ``branch``;
- ``find_open_pr`` has to be told the head repo, or a fork branch is looked for on the base and
  found nowhere (which is how revise breaks and how an update opens a second pull request);
- a revise writes with the **submitter's** token, because a GitHub App installation token cannot
  push to a personal fork — the App is not installed there.

Plus the fallback, which is the whole reason fork mode is safe to turn on: anything that stops a
fork being had puts the submission back on the bot rather than failing it.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.github import FakeGitHubClient, GitHubError, HttpGitHubClient
from app.locks import PathwayLockRegistry
from app.submit import SubmissionService
from app.submit.service import SubmissionMode
from app.submit.targets import (
    BotIdentityUnavailable,
    WriteTarget,
    resolve_write_target,
    same_repo_target,
)
from app.update import UpdateService

REPO = "wikipathways/sandbox-wp-db"
FORK = "alice/sandbox-wp-db"
FROZEN = datetime(2026, 8, 3, 18, 0, 0, tzinfo=UTC)

GOOD_GPML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    'Organism="Homo sapiens"><Graphics BoardWidth="100" BoardHeight="100"/></Pathway>'
)


def _user_client(**kw) -> FakeGitHubClient:
    return FakeGitHubClient(
        default_branches={f"{REPO}#main": "upstream-head"}, login="alice", **kw
    )


def _fork_target(user: FakeGitHubClient) -> WriteTarget:
    return resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=FakeGitHubClient(login="wikipathways-bot"),
        content_repo=REPO,
        submitter="alice",
    )


def _service(user: FakeGitHubClient, target: WriteTarget) -> SubmissionService:
    return SubmissionService(
        None,
        target.client,
        repo=REPO,
        mode=SubmissionMode.PIPELINE,
        clock=lambda: FROZEN,
        target=target,
    )


# ---- the target itself ------------------------------------------------------------------------


def test_head_is_owner_colon_branch_only_across_repos():
    same = same_repo_target(FakeGitHubClient(), REPO)
    assert same.head("my-branch") == "my-branch"
    assert same.head_repo is None
    assert same.is_cross_repo is False

    fork = WriteTarget(FakeGitHubClient(), branch_repo=FORK, content_repo=REPO, identity="fork")
    # Only the owner: a fork keeps its parent's name, and `alice/sandbox-wp-db:b` does not resolve.
    assert fork.head("my-branch") == "alice:my-branch"
    assert fork.head_repo == FORK
    assert fork.is_cross_repo is True


# ---- submitting -------------------------------------------------------------------------------


def test_submission_branches_on_the_fork_and_opens_a_cross_repo_pull_request():
    user = _user_client()
    target = _fork_target(user)
    result = _service(user, target).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert (FORK, result.branch) in user.branches
    assert (REPO, result.branch) not in user.branches  # nothing was written to the content repo
    assert (FORK, result.branch, "pathways/WP0001/WP0001.gpml") in user.files
    assert user.pull_meta[result.pr_number]["head"] == f"alice:{result.branch}"
    assert user.pull_meta[result.pr_number]["base"] == "main"
    # Recorded off GitHub's answer, so revise can find the branch again.
    assert result.head_repo == FORK


def test_the_branch_is_cut_from_upstream_not_from_the_stale_fork():
    """The guarantee the whole update flow rests on, across the fork boundary.

    A contributor who forked a year ago has a default branch a year behind. Cutting from it would
    make every submission a silent revert of everything merged since.
    """
    user = _user_client()
    target = _fork_target(user)
    # The fork has fallen behind: its own main points somewhere older.
    user.branches[(FORK, "main")] = "stale-fork-head"

    result = _service(user, target).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert user.branches[(FORK, result.branch)] == "upstream-head"


def test_the_fork_is_ensured_once_and_reused():
    user = _user_client()
    first = _fork_target(user)
    _service(user, first).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")
    second = _fork_target(user)
    _service(user, second).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert user.forks_created == [(REPO, FORK)]  # not once per submission
    assert second.branch_repo == FORK


# ---- the fallback -----------------------------------------------------------------------------


def test_a_fork_that_cannot_be_had_falls_back_to_the_bot():
    """The reason fork mode is safe to enable: a submission never dies because forking failed.

    An organisation can forbid forking and a token can be revoked between login and submission,
    so this is a routine outcome rather than an exceptional one.
    """
    user = _user_client(fail_on={"ensure_fork"})
    bot = FakeGitHubClient(default_branches={f"{REPO}#main": "upstream-head"}, login="wp-bot")

    target = resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=bot,
        content_repo=REPO,
        submitter="alice",
    )

    assert target.identity == "bot"
    assert target.client is bot
    assert target.branch_repo == REPO
    assert target.head_repo is None  # an ordinary same-repo pull request

    result = _service(user, target).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")
    assert (REPO, result.branch) in bot.branches
    assert user.forks_created == []


def test_the_fallback_happens_before_anything_is_written():
    """Which is what makes it safe — there is no partial state to reconcile."""
    user = _user_client(fail_on={"ensure_fork"})
    bot = FakeGitHubClient(default_branches={f"{REPO}#main": "upstream-head"}, login="wp-bot")
    resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=bot,
        content_repo=REPO,
        submitter="alice",
    )
    assert user.branches == {(REPO, "main"): "upstream-head"}  # only what it was seeded with
    assert user.files == {}
    assert bot.files == {}


def test_fork_failure_with_no_bot_configured_uses_the_users_own_token():
    user = _user_client(fail_on={"ensure_fork"})
    target = resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=None,
        content_repo=REPO,
        submitter="alice",
    )
    # Not an exception: on a target the submitter *can* push to, this is simply correct, and where
    # they cannot, create_branch fails with a 403 that describes the real problem.
    assert target.identity == "user"
    assert target.client is user


def test_the_owner_of_the_content_repo_never_forks_it():
    """GitHub refuses to fork a repository into the account that owns it.

    Live-configuration case, not a hypothetical: the deployment targets `marvinm2/sandbox-wp-db`
    and `marvinm2` is who tests it. Without this, every one of his submissions would take the
    bot fallback — a worse pull request than the one he can open directly, and one that would
    have made fork mode look broken while it was working.
    """
    user = FakeGitHubClient(default_branches={f"{REPO}#main": "upstream-head"}, login="marvinm2")
    target = resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=FakeGitHubClient(login="wp-bot"),
        content_repo="marvinm2/sandbox-wp-db",
        submitter="MarvinM2",  # case-insensitive: GitHub logins are
    )
    assert target.identity == "user"
    assert target.client is user
    assert target.head_repo is None
    assert user.forks_created == []


def test_bot_identity_without_a_bot_is_a_deployment_error():
    with pytest.raises(BotIdentityUnavailable):
        resolve_write_target(
            identity="bot",
            user_client=_user_client(),
            bot_client=None,
            content_repo=REPO,
            submitter="alice",
        )


# ---- updates ----------------------------------------------------------------------------------


def test_an_update_in_fork_mode_looks_for_its_pull_request_on_the_fork(session_factory):
    """`find_open_pr` defaults to the base owner, and a fork branch is not there.

    Reading None would make the re-upload path open a *second* pull request for one pathway —
    the divergence the check-out lock exists to prevent.
    """
    user = FakeGitHubClient(
        default_branches={f"{REPO}#main": "upstream-head"},
        existing_files={f"{REPO}#pathways/WP554/WP554.gpml": "blob1"},
        login="alice",
    )
    target = _fork_target(user)
    service = UpdateService(
        PathwayLockRegistry(session_factory), target.client, repo=REPO, target=target
    )

    first = service.update_pathway(wpid=554, gpml=GOOD_GPML, submitter="alice")
    assert (FORK, first.branch) in user.branches
    assert first.head_repo == FORK

    # Re-uploading while still checked out must land on the same pull request, not a new one.
    second = service.update_pathway(wpid=554, gpml=GOOD_GPML, submitter="alice")
    assert second.pr_number == first.pr_number
    assert len(user.pulls) == 1


# ---- the real client --------------------------------------------------------------------------


def test_ensure_fork_reads_the_name_off_the_response_and_waits_for_readiness():
    """Two things the real API does that a naive implementation gets wrong.

    The fork may not be named after the parent — a submitter who already has a repository of that
    name gets something else, and guessing sends every later write to the wrong place. And a 202
    means accepted, not ready: creation is asynchronous, so the repository has to be probed.
    """
    calls: list[str] = []
    probes = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx.Response(202, json={"full_name": "alice/sandbox-wp-db-1"})
        probes["n"] += 1
        if probes["n"] < 3:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"full_name": "alice/sandbox-wp-db-1"})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    # Patch the sleep out rather than waiting three real seconds.
    import app.github.client as client_module

    original = client_module.time.sleep
    client_module.time.sleep = lambda _s: None
    try:
        assert client.ensure_fork(REPO) == "alice/sandbox-wp-db-1"
    finally:
        client_module.time.sleep = original

    assert calls[0] == f"POST /repos/{REPO}/forks"
    assert calls[1:] == ["GET /repos/alice/sandbox-wp-db-1"] * 3


def test_ensure_fork_raises_when_the_fork_never_becomes_readable():
    """Which the caller turns into a bot fallback rather than a failed submission."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"full_name": FORK})
        return httpx.Response(404, json={"message": "Not Found"})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    import app.github.client as client_module

    original = client_module.time.sleep
    client_module.time.sleep = lambda _s: None
    try:
        with pytest.raises(GitHubError, match="did not become readable"):
            client.ensure_fork(REPO)
    finally:
        client_module.time.sleep = original


# ---- who writes a revise ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "identity,head_repo,expect_user",
    [
        # A branch on a personal fork can only be written by the person who owns it: a GitHub App
        # installation token cannot push there, because the App is not installed on their account.
        ("fork", FORK, True),
        ("bot", FORK, True),
        # A branch on the content repo follows the configured identity, as it always did.
        ("bot", None, False),
        ("fork", None, False),
        ("user", None, True),
    ],
)
def test_revise_writes_with_whoever_can_reach_the_branch(identity, head_repo, expect_user):
    from app.main import _writer_client_for_revise

    user = FakeGitHubClient(login="alice")
    bot = FakeGitHubClient(login="wp-bot")
    settings = Settings(submit_identity=identity, session_secret="x" * 32)

    chosen = _writer_client_for_revise(settings, user, bot, head_repo)
    assert (chosen is user) is expect_user
