"""Approval, rejection and publication against a repo that publishes through its own Actions.

The thing under test is a handshake, not a function call: the app applies a label, some workflow
it does not run does the work, and the PR comes back **closed without being merged**. Every test
here replays that sequence through FakeGitHubClient's simulators.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.curators import ConfigCurators
from app.github import FakeGitHubClient, GitHubError
from app.models import Review, ReviewStatus
from app.review.checklist import ChecklistState
from app.review.service import (
    CurationService,
    NotACurator,
    ReviewNotFound,
    parse_publish_marker,
)

REPO = "wikipathways/sandbox-wp-db"
CURATOR = "marvinm2"


def _service(session_factory, gh, **kw) -> CurationService:
    return CurationService(
        session_factory,
        gh,
        repo=REPO,
        curators=ConfigCurators([CURATOR]),
        publish_mode="pipeline",
        **kw,
    )


def _fake() -> FakeGitHubClient:
    return FakeGitHubClient(default_branches={f"{REPO}#main": "basesha"})


def _register(svc, gh, *, kind="new", wpid=None) -> int:
    pr = gh.open_pull_request(
        REPO, head="WP0001_alice_20260727-173500", base="main", title="t", body="b"
    )
    svc.register(
        pr_number=pr.number,
        wpid=wpid,
        submitter="alice",
        kind=kind,
        head_branch=pr.head_branch,
    )
    return pr.number


def _complete_checklist(svc, pr_number: int) -> None:
    review = svc.get(pr_number)
    for item in review.checklist:
        if item.get("required"):
            svc.set_checklist_item(pr_number, item["key"], ChecklistState.PASS.value)


# -- the marker parser ---------------------------------------------------------------------


def test_marker_is_parsed_out_of_a_comment():
    body = (
        '<!-- wikipathways-publish {"pr":54,"wpid":5678,"status":"published"} -->\n'
        "Published as WP5678."
    )
    assert parse_publish_marker(body) == {"pr": 54, "wpid": 5678, "status": "published"}


def test_a_comment_without_a_marker_parses_to_nothing():
    assert parse_publish_marker("Looks good to me, merging.") is None


def test_a_malformed_marker_is_not_a_crash():
    assert parse_publish_marker("<!-- wikipathways-publish {not json} -->") is None


# -- approve -------------------------------------------------------------------------------


def test_approve_labels_and_does_not_merge(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)

    review = svc.approve(pr, CURATOR)

    assert review.status == ReviewStatus.APPROVED
    assert gh.list_labels(REPO, pr) == ["accepted"]
    assert pr not in gh.merged
    assert pr not in gh.closed


def test_approve_tells_the_submitter_something_happened(session_factory):
    # The label is silent and the PR body gets rewritten, so the comment is the only signal.
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)

    svc.approve(pr, CURATOR)

    assert any("approved" in c for c in gh.issue_comments[(REPO, pr)])


def test_a_failed_label_leaves_the_review_reviewable(session_factory):
    # The label IS the mechanism here, so a failure must not leave the review sitting in
    # APPROVED waiting on a workflow that was never triggered.
    gh = _fake()
    gh.fail_on.add("add_labels")
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)

    with pytest.raises(GitHubError):
        svc.approve(pr, CURATOR)

    assert svc.get(pr).status == ReviewStatus.OPEN


def test_approve_still_refuses_a_non_curator(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)

    with pytest.raises(NotACurator):
        svc.approve(pr, "stranger")
    assert gh.list_labels(REPO, pr) == []


# -- publication ---------------------------------------------------------------------------


def test_publication_records_the_assigned_wpid(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    gh.simulate_3a(REPO, pr, wpid=5678)
    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.PUBLISHED
    assert review.wpid == 5678
    assert review.published_at is not None


def test_a_close_with_no_announcement_is_a_failure_not_a_success(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    gh.simulate_3a(REPO, pr, wpid=None)  # the observed failure mode
    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.PUBLISH_FAILED
    assert review.wpid is None
    assert "WPID" in review.decision_note


def test_an_update_keeps_its_own_id_through_publication(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh, kind="update", wpid=5636)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    gh.simulate_3a(REPO, pr, wpid=None)  # an edit needs no new id
    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.PUBLISHED
    assert review.wpid == 5636


def test_a_closed_pr_on_an_unapproved_review_is_still_just_closed(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.CLOSED


def test_the_manual_escape_hatch_reaches_published(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_3a(REPO, pr, wpid=None)
    svc.handle_pr_closed(pr, merged=False)

    review = svc.record_published_wpid(pr, 5678, CURATOR)

    assert review.status == ReviewStatus.PUBLISHED
    assert review.wpid == 5678


def test_the_escape_hatch_is_curator_only(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    with pytest.raises(NotACurator):
        svc.record_published_wpid(pr, 5678, "stranger")


# -- reconcile -----------------------------------------------------------------------------


def test_a_stuck_approval_eventually_reports_failure(session_factory):
    # The realistic case: the label goes on and the repo's dispatcher never fires.
    gh = _fake()
    svc = _service(
        session_factory,
        gh,
        publish_timeout=timedelta(minutes=30),
        reconcile_min_interval=timedelta(seconds=0),
    )
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_dispatcher_failure(REPO, pr)

    with session_factory() as s:
        s.get(Review, pr).approved_at = datetime.now(UTC) - timedelta(hours=2)
        s.commit()

    assert svc.reconcile() == 1
    review = svc.get(pr)
    assert review.status == ReviewStatus.PUBLISH_FAILED
    assert "has not published it" in review.decision_note


def test_a_recent_approval_is_left_alone(session_factory):
    gh = _fake()
    svc = _service(
        session_factory, gh, publish_timeout=timedelta(minutes=30),
        reconcile_min_interval=timedelta(seconds=0),
    )
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    assert svc.reconcile() == 0
    assert svc.get(pr).status == ReviewStatus.APPROVED


def test_removing_the_label_puts_it_back_in_the_queue(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(seconds=0))
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.remove_label(REPO, pr, "accepted")

    assert svc.reconcile() == 1
    review = svc.get(pr)
    assert review.status == ReviewStatus.OPEN
    assert "removed" in review.decision_note


def test_reconcile_refreshes_the_labels_it_sees(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(seconds=0))
    pr = _register(svc, gh)
    gh.add_labels(REPO, pr, ["tests passed", "review required"])

    svc.reconcile()

    assert svc.get(pr).github_labels == ["review required", "tests passed"]


def test_the_throttle_stops_a_second_check(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(minutes=5))
    pr = _register(svc, gh)
    gh.add_labels(REPO, pr, ["tests passed"])

    svc.reconcile()
    gh.fail_on.add("get_pull_request")  # a second look would blow up

    assert svc.reconcile() == 0


# -- reject --------------------------------------------------------------------------------


def test_reject_comments_before_it_labels(session_factory):
    # The rejection workflow deletes the drafts, so the reason has to be on the record first.
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    svc.reject(pr, CURATOR, note="Duplicate of WP1234.")

    assert gh.issue_comments[(REPO, pr)]
    assert ("Duplicate of WP1234." in gh.issue_comments[(REPO, pr)][0])
    assert gh.label_log[-1] == (REPO, pr, "add", "rejected")


def test_reject_is_terminal_and_keeps_the_reason(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    review = svc.reject(pr, CURATOR, note="Out of scope.")

    assert review.status == ReviewStatus.REJECTED
    assert review.decision_note == "Out of scope."
    assert review.decided_by == CURATOR


def test_the_repos_rejection_workflow_does_not_undo_the_rejection(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(seconds=0))
    pr = _register(svc, gh)
    svc.reject(pr, CURATOR)

    gh.simulate_3b(REPO, pr)
    svc.reconcile()

    assert svc.get(pr).status == ReviewStatus.REJECTED


def test_reject_is_curator_only(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    with pytest.raises(NotACurator):
        svc.reject(pr, "stranger")
    assert gh.list_labels(REPO, pr) == []


def test_reject_on_an_unknown_pr_is_a_404(session_factory):
    svc = _service(session_factory, _fake())
    with pytest.raises(ReviewNotFound):
        svc.reject(999, CURATOR)


# -- labels applied on GitHub directly -----------------------------------------------------


def test_a_curator_labelling_on_github_moves_the_review(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    review = svc.handle_label_event(pr, "accepted", added=True, actor="egonw")

    assert review.status == ReviewStatus.APPROVED
    assert review.approved_by == "egonw"
    assert "incomplete checklist" in review.decision_note


def test_a_complete_checklist_leaves_no_discrepancy_note(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)

    review = svc.handle_label_event(pr, "accepted", added=True, actor="egonw")

    assert review.decision_note is None


def test_our_own_label_echo_is_a_no_op(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    assert svc.handle_label_event(pr, "accepted", added=True, actor=CURATOR) is None
    assert svc.get(pr).approved_by == CURATOR


def test_unlabelling_accepted_reopens_the_review(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    review = svc.handle_label_event(pr, "accepted", added=False, actor="egonw")

    assert review.status == ReviewStatus.OPEN


def test_an_unrelated_label_changes_nothing(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    assert svc.handle_label_event(pr, "tests passed", added=True, actor="egonw") is None
    assert svc.get(pr).status == ReviewStatus.OPEN
