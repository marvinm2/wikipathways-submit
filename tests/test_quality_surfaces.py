"""The three places the graded report shows up: the submit form, the review card, the PR comment."""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.github import FakeGitHubClient
from app.main import (
    build_app,
    get_bot_client,
    get_bot_optional,
    get_current_user,
    get_github_client,
)
from app.preview.service import PreviewService
from app.quality import inspect_gpml
from app.review.service import render_mirror_comment
from tests.test_quality_rules import GOOD, POOR


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        dev_wpid_floor=5636,
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        preview_cache_dir=str(tmp_path / "preview-cache"),
    )
    with TestClient(build_app(settings)) as c:
        yield c


def _upload(content: str, name: str = "pathway.gpml"):
    return {"file": (name, io.BytesIO(content.encode()), "application/xml")}


# ---- the submit form -------------------------------------------------------------------------


def test_validate_returns_the_graded_report_not_just_a_verdict(client):
    r = client.post("/api/validate", files=_upload(POOR))
    assert r.status_code == 200
    ids = {f["id"] for f in r.json()["quality"]["findings"]}
    # The three the repository will also test for itself, plus the crasher it dies on.
    assert {"content.title_length", "content.description", "gpml.citation_ids"} <= ids


def test_validate_still_refuses_what_it_always_refused(client):
    r = client.post("/api/validate", files=_upload("<html>nope</html>"))
    assert r.status_code == 422
    assert r.json()["detail"]["errors"] == ["no <Pathway> root element found"]


def test_a_clean_pathway_reports_no_problems(client):
    findings = client.post("/api/validate", files=_upload(GOOD)).json()["quality"]["findings"]
    loud = [f for f in findings if f["severity"] in ("warn", "fail", "block")]
    assert loud == [], loud


def test_the_findings_say_which_ones_the_repository_will_repeat(client):
    findings = client.post("/api/validate", files=_upload(POOR)).json()["quality"]["findings"]
    predicted = {f["id"] for f in findings if f["predicts_repo"]}
    assert "content.title_length" in predicted
    # The portal's own checks are not claimed to be the repository's.
    assert "gpml.citation_ids" not in predicted


# ---- the cached sidecar ----------------------------------------------------------------------


def test_the_report_is_cached_beside_the_render(tmp_path):
    previews = PreviewService(cache_dir=tmp_path)
    previews.render_local(7, 1, after_gpml=GOOD.encode())
    report = previews.quality(7)
    assert report is not None
    assert report.by_id("content.title_length").severity == "pass"


def test_the_renderers_verdict_is_recorded_where_only_it_knows_the_answer(tmp_path):
    """``app.quality`` will not run the renderer itself, so this is the only path that fills it."""
    previews = PreviewService(cache_dir=tmp_path)
    previews.render_local(8, 1, after_gpml=GOOD.encode())
    assert previews.quality(8).by_id("render.drawable").severity == "pass"


def test_a_file_that_is_not_a_pathway_is_reported_as_that_and_nothing_else(tmp_path):
    """Every later rule would be describing a document that is not a pathway."""
    previews = PreviewService(cache_dir=tmp_path)
    previews.render_local(10, 1, after_gpml=b"<html>not a pathway</html>")
    report = previews.quality(10)
    assert [f.id for f in report.findings] == ["gpml.root"]


def test_an_update_is_graded_against_its_base_version(tmp_path):
    previews = PreviewService(cache_dir=tmp_path)
    previews.render_local(9, 1, after_gpml=GOOD.encode(), before_gpml=GOOD.encode())
    finding = previews.quality(9).by_id("content.datanode_changes")
    # An update whose nodes are identical is the "pass" arm, not the new-pathway "na" arm.
    assert finding.severity == "pass"


def test_nothing_rendered_means_no_report_rather_than_an_empty_one(tmp_path):
    """An empty panel reads as "no problems found", which is a different claim."""
    assert PreviewService(cache_dir=tmp_path).quality(404) is None


def test_a_cache_written_before_quality_existed_still_loads(tmp_path):
    previews = PreviewService(cache_dir=tmp_path)
    previews.render_local(11, 1, after_gpml=GOOD.encode())
    (tmp_path / "11" / "quality.json").unlink()
    assert previews.quality(11) is None


# ---- the mirror comment ----------------------------------------------------------------------


class _Review:
    pr_number = 3
    wpid_str = "WP0001"
    kind = "new"
    submitter = "bob"
    assigned_curator = None
    submitter_note = ""
    approved_by = None
    decision_note = None
    checklist: list = []

    class status:
        value = "open"


def test_the_comment_carries_the_table_when_a_report_is_cached():
    body = render_mirror_comment(
        _Review(), "wikipathways/sandbox-wp-db", quality=inspect_gpml(POOR)
    )
    assert "What the portal measured" in body
    assert "| Status | Check | Detail |" in body


def test_a_comment_with_no_report_is_the_one_it_always_was():
    body = render_mirror_comment(_Review(), "wikipathways/sandbox-wp-db")
    assert "What the portal measured" not in body
    assert "| Check | State | Notes |" in body


def test_gpml_element_names_survive_githubs_markdown():
    """`<bp:ID>` is an unknown HTML tag to GitHub and gets dropped, taking the sentence with it."""
    body = render_mirror_comment(
        _Review(), "wikipathways/sandbox-wp-db", quality=inspect_gpml(POOR)
    )
    assert "&lt;bp:ID&gt;" in body
    assert "<bp:ID>" not in body


def test_the_mirror_carries_the_report_from_its_very_first_post(tmp_path):
    """``register`` posts the mirror and reads the report out of the render cache.

    So the render has to happen before the review is registered. With the original order the
    first comment on every pull request had the table missing, and it only appeared whenever a
    curator next touched the checklist — on the one artifact a GitHub-native reviewer sees.
    """
    settings = Settings(
        _env_file=None,
        dev_wpid_floor=5636,
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        preview_cache_dir=str(tmp_path / "preview-cache"),
    )
    app = build_app(settings)
    fake = FakeGitHubClient(
        default_branches={f"{settings.content_repo}#{settings.default_branch}": "basesha"}
    )
    app.dependency_overrides[get_github_client] = lambda: fake
    app.dependency_overrides[get_bot_optional] = lambda: fake
    app.dependency_overrides[get_bot_client] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: "alice"
    with TestClient(app) as c:
        pr = c.post("/api/submit", files=_upload(POOR)).json()["pr_number"]
    posted = list(fake.comments[(settings.content_repo, pr)].values())
    assert posted, "no mirror comment was posted at all"
    assert "What the portal measured" in posted[0]
