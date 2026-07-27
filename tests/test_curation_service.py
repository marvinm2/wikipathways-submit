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


def _service(session_factory, github=None, allocator=None, locks=None) -> CurationService:
    return CurationService(
        session_factory,
        github,
        repo=REPO,
        curators=ConfigCurators(CURATORS),
        allocator=allocator,
        locks=locks,
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
    assert "### WikiPathways curation — WP5639" in body
    # House style: no decorative emoji in content posted to a real PR.
    for emoji in ("🧬", "✅", "❌", "➖", "⬜"):
        assert emoji not in body


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
