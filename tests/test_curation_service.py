from __future__ import annotations

import httpx
import pytest

from app.curators import ConfigCurators
from app.github import FakeGitHubClient, GitHubError
from app.locks import PathwayLockRegistry
from app.models import ReservationStatus, ReviewStatus, WpidReservation
from app.review.checklist import CURATION_CHECKLIST
from app.review.service import (
    ChecklistIncomplete,
    CurationService,
    NotACurator,
    PreviewNotReady,
    ReviewNotFound,
)
from app.wpid import WpidAllocator

REPO = "wikipathways/wikipathways-database"
CURATORS = {"curator", "alice"}

REQUIRED_KEYS = [i.key for i in CURATION_CHECKLIST if i.required]


@pytest.fixture
def allocator(session_factory):
    return WpidAllocator(session_factory, floor_provider=lambda: 5636)


@pytest.fixture
def locks(session_factory):
    return PathwayLockRegistry(session_factory)


def _service(
    session_factory, github=None, allocator=None, locks=None, app_base_url=""
) -> CurationService:
    return CurationService(
        session_factory,
        github,
        repo=REPO,
        curators=ConfigCurators(CURATORS),
        allocator=allocator,
        locks=locks,
        app_base_url=app_base_url,
    )


def _complete_required(svc: CurationService, pr_number: int) -> None:
    for key in REQUIRED_KEYS:
        svc.set_checklist_item(pr_number, key, "pass")


def test_register_is_idempotent_and_queue_lists_open(session_factory):
    svc = _service(session_factory)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")  # no duplicate
    svc.register(pr_number=2, wpid=5638, submitter="carol", kind="update")
    queue = svc.list_queue()
    assert [r.pr_number for r in queue] == [1, 2]
    assert queue[0].status == ReviewStatus.OPEN
    # Fresh review starts with the full checklist, all pending.
    assert len(queue[0].checklist) == len(CURATION_CHECKLIST)
    assert all(item["state"] == "pending" for item in queue[0].checklist)


def test_get_missing_raises(session_factory):
    with pytest.raises(ReviewNotFound):
        _service(session_factory).get(999)


def test_state_click_keeps_the_existing_note(session_factory):
    # The dashboard's Pass/Fail/N/A chips send no note. Treating that as an empty note wiped the
    # auto-derived explanation the curator is reading ("1 of 3 data nodes have no identifier").
    svc = _service(session_factory)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.set_checklist_item(1, "render_ok", "pending", note="1 of 3 data nodes unannotated")
    svc.set_checklist_item(1, "render_ok", "pass")  # a state click, not a note edit
    item = next(i for i in svc.get(1).checklist if i["key"] == "render_ok")
    assert item["state"] == "pass"
    assert item["note"] == "1 of 3 data nodes unannotated"
    # An explicit empty string still clears it — that is a deliberate edit.
    svc.set_checklist_item(1, "render_ok", "pass", note="")
    assert next(i for i in svc.get(1).checklist if i["key"] == "render_ok")["note"] == ""


def test_set_checklist_item_validates(session_factory):
    svc = _service(session_factory)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.set_checklist_item(1, "render_ok", "pass", note="looks good")
    review = svc.get(1)
    item = next(i for i in review.checklist if i["key"] == "render_ok")
    assert item["state"] == "pass"
    assert item["note"] == "looks good"
    with pytest.raises(ValueError):
        svc.set_checklist_item(1, "nonexistent", "pass")
    with pytest.raises(ValueError):
        svc.set_checklist_item(1, "render_ok", "not-a-state")


def test_concurrent_checklist_updates_all_persist(session_factory):
    # Regression for issue #15: setting distinct checklist items concurrently must not lose
    # updates. Each write is a read-modify-write of the whole JSON blob; without the row lock +
    # retry, interleaved writes overwrite each other and only the last survives.
    import threading

    svc = _service(session_factory)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")

    keys = [item.key for item in CURATION_CHECKLIST]  # every item, distinct
    barrier = threading.Barrier(len(keys))
    errors: list[Exception] = []

    def worker(key: str) -> None:
        try:
            barrier.wait()  # maximise interleaving
            svc.set_checklist_item(1, key, "pass", note=f"set-{key}")
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    review = svc.get(1)
    states = {item["key"]: item["state"] for item in review.checklist}
    notes = {item["key"]: item["note"] for item in review.checklist}
    # Every concurrently-set item survived — no lost update.
    assert all(states[k] == "pass" for k in keys), states
    assert all(notes[k] == f"set-{k}" for k in keys), notes


def test_assign_requests_pr_reviewer_on_github(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    review = svc.assign(1, "curator")
    assert review.assigned_curator == "curator"
    assert gh.review_requests.get(1) == ["curator"]  # real PR review request too


def test_assign_swallows_review_request_failure(session_factory):
    # GitHub declines (e.g. can't request review from the PR author) → app assignment still holds.
    gh = FakeGitHubClient(fail_on={"request_pr_reviewer"})
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    review = svc.assign(1, "curator")  # does not raise
    assert review.assigned_curator == "curator"
    assert gh.review_requests == {}


def test_request_changes_sets_status_and_posts_comment(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    review = svc.request_changes(1, "curator", note="Annotate the AKT1 node.")
    assert review.status == ReviewStatus.CHANGES_REQUESTED
    comments = gh.issue_comments[(REPO, 1)]
    assert len(comments) == 1
    assert "Changes requested" in comments[0]
    assert "@curator" in comments[0]
    assert "Annotate the AKT1 node." in comments[0]


def test_find_open_new_review_and_revise_rebuilds_checklist(session_factory):
    from app.preview.metadata import parse_curation_metadata

    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.request_changes(1, "curator")
    assert svc.find_open_new_review(5637).pr_number == 1

    revised = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="X" Organism="Homo sapiens">'
        '<DataNode TextLabel="INSR"><Xref Database="Ensembl" ID="ENSG00000171105"/></DataNode>'
        "</Pathway>"
    )
    review = svc.revise(1, metadata=parse_curation_metadata(revised))
    assert review.status == ReviewStatus.OPEN  # re-opened
    dn = next(i for i in review.checklist if i["key"] == "datanodes_mapped")
    assert dn["state"] == "pass" and dn["auto"] is True  # checklist rebuilt from new content


def test_reupload_after_changes_requested_reopens_review(session_factory):
    svc = _service(session_factory)  # no github → comment step is skipped
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.request_changes(1, "curator")
    assert svc.get(1).status == ReviewStatus.CHANGES_REQUESTED
    # A re-upload re-registers the same PR → back into the review queue.
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="update")
    assert svc.get(1).status == ReviewStatus.OPEN


def _gated_service(session_factory, github):
    return CurationService(
        session_factory,
        github,
        repo=REPO,
        curators=ConfigCurators(CURATORS),
        require_preview_check=True,
        preview_workflow_file="pr-preview.yml",
        preview_artifact_name="pr-preview",
    )


def test_approve_blocked_until_preview_ready(session_factory):
    gh = FakeGitHubClient(previews={1: {"status": "pending"}})
    svc = _gated_service(session_factory, gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    _complete_required(svc, 1)

    # Checklist complete, but the PR-preview CI has not gone green → merge is refused.
    with pytest.raises(PreviewNotReady):
        svc.approve_and_merge(1, "curator")
    assert gh.merged == set()

    # Once the preview is ready, the same approval merges.
    gh.previews[1] = {"status": "ready"}
    review = svc.approve_and_merge(1, "curator")
    assert review.status == ReviewStatus.MERGED
    assert gh.merged == {1}


def test_reconcile_terminalises_out_of_band_prs(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    # Open PRs 1..3 in the fake so it knows their state; register a review for each, plus a
    # review (#4) whose PR was never opened (deleted / absent).
    for wpid in (5637, 5638, 5639):
        gh.open_pull_request(REPO, head=f"submit/WP{wpid}", base="main", title="t", body="b")
    for pr, wpid in ((1, 5637), (2, 5638), (3, 5639), (4, 5640)):
        svc.register(pr_number=pr, wpid=wpid, submitter="bob", kind="new")

    gh.merged.add(1)  # merged outside the app
    gh.closed.add(2)  # closed unmerged outside the app
    # 3 stays open; 4 is absent (no PR) → treated as closed

    assert svc.reconcile_open_reviews() == 3
    assert svc.get(1).status == ReviewStatus.MERGED
    assert svc.get(2).status == ReviewStatus.CLOSED
    assert svc.get(3).status == ReviewStatus.OPEN
    assert svc.get(4).status == ReviewStatus.CLOSED
    # Idempotent: a second pass reconciles nothing new.
    assert svc.reconcile_open_reviews() == 0


def test_approve_requires_curator(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    _complete_required(svc, 1)
    with pytest.raises(NotACurator):
        svc.approve_and_merge(1, "randomuser")
    assert gh.merged == set()  # not merged


def test_approve_requires_complete_checklist(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    # Leave one required item pending.
    for key in REQUIRED_KEYS[:-1]:
        svc.set_checklist_item(1, key, "pass")
    with pytest.raises(ChecklistIncomplete):
        svc.approve_and_merge(1, "curator")
    assert gh.merged == set()


def test_approve_merges_and_completes_lifecycle(session_factory, allocator, locks):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh, allocator=allocator, locks=locks)

    # A real submission: WPID reserved, pathway locked, review opened.
    wpid = allocator.allocate("bob")  # 5637
    locks.acquire(wpid, "bob")
    svc.register(pr_number=7, wpid=wpid, submitter="bob", kind="new")
    _complete_required(svc, 7)

    review = svc.approve_and_merge(7, "curator")

    assert review.status == ReviewStatus.MERGED
    assert review.approved_by == "curator"
    assert review.merged_at is not None
    # PR merged on GitHub.
    assert gh.merged == {7}
    # WPID reservation promoted to permanent.
    with session_factory() as s:
        assert s.get(WpidReservation, wpid).status == ReservationStatus.MERGED
    # Pathway lock released.
    assert not locks.is_locked(wpid)


def test_mirror_comment_is_best_effort(session_factory):
    # A comment failure must not sink the primary action (register / checklist / approve).
    gh = FakeGitHubClient(fail_on={"upsert_issue_comment"})
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")  # does not raise
    svc.set_checklist_item(1, "render_ok", "pass")  # does not raise
    assert svc.get(1).status == ReviewStatus.OPEN
    assert gh.comments == {}  # nothing recorded because every upsert failed


class _TransportFailingClient(FakeGitHubClient):
    """A bot client whose comment API fails at the transport layer (not a GitHubError)."""

    def upsert_issue_comment(self, repo, issue_number, body, *, marker):
        raise httpx.ConnectError("connection refused")


def test_mirror_comment_swallows_transport_errors(session_factory, allocator, locks):
    # A network blip talking to the comments API must NOT fail an action that already succeeded.
    gh = _TransportFailingClient()
    svc = _service(session_factory, github=gh, allocator=allocator, locks=locks)
    wpid = allocator.allocate("bob")
    locks.acquire(wpid, "bob")
    svc.register(pr_number=7, wpid=wpid, submitter="bob", kind="new")  # does not raise
    _complete_required(svc, 7)
    review = svc.approve_and_merge(7, "curator")  # merge succeeded; mirror failed silently
    assert review.status == ReviewStatus.MERGED
    assert gh.merged == {7}


def test_mirror_comment_written_when_bot_present(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=3, wpid=5639, submitter="bob", kind="new")
    body = gh.comments[(REPO, 3)]["<!-- wikipathways-submit:mirror -->"]
    assert "WP5639" in body and "read-only" in body
    assert body.startswith("<!-- wikipathways-submit:mirror -->")
    # Marked as a bot message (robot marker + automated line), so GitHub-native reviewers know
    # it is not a human curator's comment.
    assert "### 🤖 WikiPathways curation — WP5639" in body
    assert "Automated message from the curation bot" in body
    # House style: no decorative emoji beyond the bot marker.
    for emoji in ("🧬", "✅", "❌", "➖", "⬜"):
        assert emoji not in body


def test_mirror_comment_links_to_the_render_when_a_public_url_is_set(session_factory):
    # CI publishes no image, so the mirror comment is the only thing that can point a
    # GitHub-native reviewer at where the before/after render actually lives.
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh, app_base_url="https://curator.example.org/")
    svc.register(pr_number=3, wpid=5639, submitter="bob", kind="new")
    body = gh.comments[(REPO, 3)]["<!-- wikipathways-submit:mirror -->"]
    assert "https://curator.example.org/dashboard/3" in body


def test_mirror_comment_omits_the_render_link_without_a_public_url(session_factory):
    # Local dev: better no link than one pointing at somebody's localhost.
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=3, wpid=5639, submitter="bob", kind="new")
    body = gh.comments[(REPO, 3)]["<!-- wikipathways-submit:mirror -->"]
    assert "Before/after render:" not in body


def test_approve_does_not_mutate_state_if_merge_fails(session_factory, allocator, locks):
    gh = FakeGitHubClient(fail_on={"merge_pull_request"})
    svc = _service(session_factory, github=gh, allocator=allocator, locks=locks)
    wpid = allocator.allocate("bob")
    locks.acquire(wpid, "bob")
    svc.register(pr_number=7, wpid=wpid, submitter="bob", kind="new")
    _complete_required(svc, 7)

    with pytest.raises(GitHubError):
        svc.approve_and_merge(7, "curator")

    # Merge failed → review still OPEN, lock still held, reservation still just RESERVED.
    assert svc.get(7).status == ReviewStatus.OPEN
    assert locks.is_locked(wpid)
    with session_factory() as s:
        assert s.get(WpidReservation, wpid).status == ReservationStatus.RESERVED
