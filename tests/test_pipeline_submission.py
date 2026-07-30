"""Submitting into a target repo that publishes through its own Actions.

The contract being tested is with workflows this app does not run (docs/sandbox-pipeline.md), so
the assertions that matter are the ones about what the *other side* will see: the file's
basename, the branch name, and the GPML's own Version attribute.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from app.github import BranchAlreadyExists, FakeGitHubClient, GitHubError
from app.models import WpidReservation
from app.submit import SubmissionService
from app.submit.service import SubmissionMode

REPO = "wikipathways/sandbox-wp-db"

GOOD_GPML = (
    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    'Organism="Homo sapiens" Version="WP1_r00000000000000"></Pathway>'
)

#: The target repo's own new-vs-edit classifier, copied verbatim from its
#: `1_on_pull_request.yml` `get-gpml` step. A basename that matches is treated as an edit of an
#: existing pathway; anything else is a new submission. Pinned here because our placeholder is
#: only correct *relative to this regex*, and nothing else in either repo would catch a change
#: to it: a relaxed pattern would silently turn every new submission into an edit of WP1.
UPSTREAM_EDIT_RE = re.compile(r"^WP[1-9][0-9]{0,4}\.gpml$")

FROZEN = datetime(2026, 7, 27, 17, 35, 0, tzinfo=UTC)


def _fake_github(**kw) -> FakeGitHubClient:
    return FakeGitHubClient(default_branches={f"{REPO}#main": "basesha123"}, **kw)


def _service(gh, *, clock=lambda: FROZEN) -> SubmissionService:
    # No allocator: pipeline mode must never need one.
    return SubmissionService(None, gh, repo=REPO, mode=SubmissionMode.PIPELINE, clock=clock)


def test_branch_carries_the_placeholder_submitter_and_timestamp():
    gh = _fake_github()
    result = _service(gh).submit_new_pathway(gpml=GOOD_GPML, submitter="marvinm2")

    assert result.branch == "WP0001_marvinm2_20260727-173500"
    assert (REPO, result.branch) in gh.branches


def test_file_lands_at_the_placeholder_path():
    gh = _fake_github()
    result = _service(gh).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert result.path == "pathways/WP0001/WP0001.gpml"
    assert (REPO, result.branch, "pathways/WP0001/WP0001.gpml") in gh.files


def test_a_placeholder_left_on_the_base_branch_does_not_block_the_next_submission():
    """The placeholder path is shared by everyone, so it is not reliably free.

    On a healthy target it never survives on `main` — publication moves it aside and closes the
    pull request unmerged. But merging a pipeline pull request by hand (which happened live on
    2026-07-30, after the publish workflow's own close step lost a race with a manual merge)
    commits the placeholder to `main`, and from that moment a create-only write answers
    422 `"sha" wasn't supplied` for *every* submitter. Overwrite instead of refusing.
    """
    gh = _fake_github(existing_files={f"{REPO}#pathways/WP0001/WP0001.gpml": "staleblob"})

    result = _service(gh).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert result.path == "pathways/WP0001/WP0001.gpml"
    content = gh.files[(REPO, result.branch, result.path)][0]
    assert "Mitophagy" in content


def test_placeholder_basename_is_not_an_edit_upstream():
    # The whole reason the placeholder is WP0001 and not WP1.
    assert not UPSTREAM_EDIT_RE.match("WP0001.gpml")
    assert UPSTREAM_EDIT_RE.match("WP1.gpml")
    assert UPSTREAM_EDIT_RE.match("WP5637.gpml")


def test_gpml_version_matches_the_placeholder_filename():
    gh = _fake_github()
    result = _service(gh).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    content = gh.files[(REPO, result.branch, result.path)][0]
    assert 'Version="WP0001_r' in content
    # Never WP1 — that is a different, real pathway.
    assert 'Version="WP1_' not in content


def test_no_wpid_is_claimed(session_factory):
    gh = _fake_github()
    result = _service(gh).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert result.wpid is None
    assert result.wpid_str == "WP0001"
    with session_factory() as s:
        assert s.query(WpidReservation).count() == 0


def test_username_is_sanitised_into_a_valid_branch_name():
    gh = _fake_github()
    result = _service(gh).submit_new_pathway(gpml=GOOD_GPML, submitter="a/weird user")

    assert result.branch == "WP0001_a-weird-user_20260727-173500"


def test_same_second_collision_steps_to_a_suffix():
    gh = _fake_github()
    svc = _service(gh)
    first = svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")
    second = svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert first.branch == "WP0001_alice_20260727-173500"
    assert second.branch == "WP0001_alice_20260727-173500-2"


def test_collisions_beyond_the_retry_budget_raise():
    gh = _fake_github()
    svc = _service(gh)
    for _ in range(3):
        svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    with pytest.raises(BranchAlreadyExists):
        svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")


def test_pr_title_names_the_pathway_and_the_submitter():
    # The title is the durable field: the target repo's workflow rewrites the body twice.
    gh = _fake_github()
    result = _service(gh).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert gh.pull_meta[result.pr_number]["title"] == (
        "[NEW] Mitophagy — submitted by @alice"
    )


def test_pr_body_does_not_promise_a_wpid():
    gh = _fake_github()
    result = _service(gh).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    body = gh.pull_meta[result.pr_number]["body"]
    assert "not yet assigned" in body
    assert "WP0001" not in body


def test_the_submitter_is_the_commit_author():
    # In pipeline mode the bot pushes the branch, so authorship has to come from the commit.
    gh = _fake_github()
    svc = _service(gh)

    captured: dict = {}
    original = gh.put_file

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    gh.put_file = spy  # type: ignore[method-assign]
    svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice", author_email="alice@example.org")

    assert captured["author_name"] == "alice"
    assert captured["author_email"] == "alice@example.org"


def test_a_failed_submission_leaves_no_reservation(session_factory):
    gh = _fake_github(fail_on={"open_pull_request"})

    with pytest.raises(GitHubError):
        _service(gh).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    with session_factory() as s:
        assert s.query(WpidReservation).count() == 0
