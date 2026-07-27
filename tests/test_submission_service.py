from __future__ import annotations

import pytest

from app.github import FakeGitHubClient
from app.models import WpidReservation
from app.submit import InvalidGpml, SubmissionService
from app.wpid import WpidAllocator

REPO = "wikipathways/wikipathways-database"

GOOD_GPML = (
    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    'Organism="Homo sapiens" Version="WP1_r00000000000000"></Pathway>'
)
BAD_GPML = "<html>not a pathway</html>"


@pytest.fixture
def allocator(session_factory):
    return WpidAllocator(session_factory, floor_provider=lambda: 5636)


def _fake_github(**kw) -> FakeGitHubClient:
    return FakeGitHubClient(default_branches={f"{REPO}#main": "basesha123"}, **kw)


def _reservations(session_factory) -> list[WpidReservation]:
    with session_factory() as s:
        return list(s.query(WpidReservation).all())


def test_submit_new_pathway_happy_path(allocator, session_factory):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)

    result = svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert result.wpid == 5637
    assert result.wpid_str == "WP5637"
    assert result.branch == "submit/WP5637"
    assert result.path == "pathways/WP5637/WP5637.gpml"
    assert result.pr_number == 1
    assert result.pr_url.endswith("/pull/1")

    # Branch created off the base SHA.
    assert gh.branches[(REPO, "submit/WP5637")] == "basesha123"
    # File committed at the canonical path with the WPID stamped into Version.
    content, message, _sha = gh.files[(REPO, "submit/WP5637", "pathways/WP5637/WP5637.gpml")]
    assert 'Version="WP5637_r' in content
    assert "WP1_r00000000000000" not in content  # placeholder overwritten
    assert message == "Add WP5637: Mitophagy"
    # PR opened, with the human-facing title/body a reviewer sees.
    assert gh.pulls[0].head_branch == "submit/WP5637"
    meta = gh.pull_meta[1]
    assert meta["title"] == "New pathway WP5637: Mitophagy"
    assert meta["base"] == "main"
    assert meta["head"] == "submit/WP5637"
    assert "**WPID:** WP5637 (assigned by the app)" in meta["body"]
    assert "**Organism:** Homo sapiens" in meta["body"]
    assert "**Submitter:** @alice" in meta["body"]
    # No note was supplied, so the body carries no submitter-note section.
    assert "Submitter note" not in meta["body"]

    # Reservation persisted with the PR number attached.
    rows = _reservations(session_factory)
    assert len(rows) == 1
    assert rows[0].wpid == 5637
    assert rows[0].pr_number == 1


def test_submit_description_flows_into_pr_body(allocator):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)

    svc.submit_new_pathway(
        gpml=GOOD_GPML,
        submitter="alice",
        description="Curated from Reactome R-HSA-1234; two data nodes need HGNC ids.",
    )

    body = gh.pull_meta[1]["body"]
    assert "**Submitter note**" in body
    assert "Curated from Reactome R-HSA-1234" in body


def test_submit_blank_description_adds_no_note(allocator):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)

    svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice", description="   ")

    assert "Submitter note" not in gh.pull_meta[1]["body"]


def test_submit_invalid_gpml_reserves_nothing(allocator, session_factory):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)

    with pytest.raises(InvalidGpml):
        svc.submit_new_pathway(gpml=BAD_GPML, submitter="alice")

    # Validation precedes allocation, so no WPID was consumed and no GitHub calls happened.
    assert _reservations(session_factory) == []
    assert gh.pulls == []
    assert not gh.files


def test_github_failure_rolls_back_wpid(allocator, session_factory):
    gh = _fake_github(fail_on={"open_pull_request"})
    svc = SubmissionService(allocator, gh, repo=REPO)

    with pytest.raises(Exception):  # noqa: B017 - GitHubError surfaces
        svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    # WPID returned to the pool: no reservation left, and the next submit reuses 5637.
    assert _reservations(session_factory) == []
    gh2 = _fake_github()
    svc2 = SubmissionService(allocator, gh2, repo=REPO)
    assert svc2.submit_new_pathway(gpml=GOOD_GPML, submitter="bob").wpid == 5637


def test_submits_get_incrementing_wpids(allocator):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)
    first = svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")
    second = svc.submit_new_pathway(gpml=GOOD_GPML, submitter="bob")
    assert (first.wpid, second.wpid) == (5637, 5638)
    assert {b[1] for b in gh.branches} == {"main", "submit/WP5637", "submit/WP5638"}
