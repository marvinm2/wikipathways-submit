"""The HTTP surface in pipeline mode: submitting into a repo that publishes via its own Actions."""
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
from app.models import Review
from tests.test_api import _login

REPO = "wikipathways/sandbox-wp-db"

GOOD_GPML = (
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    b'Organism="Homo sapiens" Version="WP5636_r20260520113005"></Pathway>'
)


def _pipeline_app(tmp_path, *, curators=(), fake=None):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        content_repo=REPO,
        publish_mode="pipeline",
        submit_identity="bot",
        # The target repo has no pr-preview.yml; leaving this on would 409 every approval.
        require_preview_check=False,
        curators=list(curators),
        preview_cache_dir=str(tmp_path / "preview-cache"),
    )
    app = build_app(settings)
    fake = fake or FakeGitHubClient(default_branches={f"{REPO}#main": "basesha"})
    app.dependency_overrides[get_github_client] = lambda: fake
    app.dependency_overrides[get_bot_optional] = lambda: fake
    app.dependency_overrides[get_bot_client] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: "marvinm2"
    app.state._fake = fake
    return app


@pytest.fixture
def pipeline_client(tmp_path):
    app = _pipeline_app(tmp_path)
    with TestClient(app) as c:
        yield c


def _submit(client) -> dict:
    resp = client.post(
        "/api/submit",
        files={"file": ("mito.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        data={"description": "A first pass at mitophagy."},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_submit_reports_the_placeholder_not_an_assigned_id(pipeline_client):
    body = _submit(pipeline_client)

    assert body["wpid"] == "WP0001"
    assert body["path"] == "pathways/WP0001/WP0001.gpml"
    assert body["pr_number"] == 1


def test_submit_records_the_branch_and_no_wpid(pipeline_client, tmp_path):
    body = _submit(pipeline_client)
    factory = pipeline_client.app.state.session_factory

    with factory() as s:
        review = s.get(Review, body["pr_number"])
    assert review.wpid is None
    assert review.wpid_str == "WP0001 (unassigned)"
    assert review.head_branch.startswith("WP0001_marvinm2_")


def test_submit_labels_the_pr_with_the_repos_own_vocabulary(pipeline_client):
    body = _submit(pipeline_client)
    fake = pipeline_client.app.state._fake

    assert fake.list_labels(REPO, body["pr_number"]) == ["new pathway submission"]


def test_a_label_failure_does_not_cost_the_submission(tmp_path):
    # Descriptive labels are best-effort; only `accepted` is load-bearing.
    fake = FakeGitHubClient(
        default_branches={f"{REPO}#main": "basesha"}, fail_on={"add_labels"}
    )
    with TestClient(_pipeline_app(tmp_path, fake=fake)) as client:
        body = _submit(client)

    assert body["pr_number"] == 1
    assert fake.list_labels(REPO, 1) == []


def test_validate_advertises_the_placeholder_path(pipeline_client):
    resp = pipeline_client.post(
        "/api/validate",
        files={"file": ("mito.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
    )

    assert resp.json()["will_layout_to"] == "pathways/WP0001/WP0001.gpml"


def test_the_queue_shows_a_submission_with_no_wpid(pipeline_client):
    _submit(pipeline_client)
    rows = pipeline_client.get("/api/reviews").json()

    assert len(rows) == 1
    assert rows[0]["wpid"] is None


def test_mirror_comment_names_the_placeholder(pipeline_client):
    body = _submit(pipeline_client)
    fake = pipeline_client.app.state._fake

    comment = next(iter(fake.comments[(REPO, body["pr_number"])].values()))
    assert "WP0001 (unassigned)" in comment


def test_a_failed_pipeline_run_is_reported_in_the_queue(pipeline_client):
    # The submitter loses their metadata tables and draft page when the target repo cannot read
    # the GPML, and the only other trace is a job buried in the Actions tab.
    _submit(pipeline_client)
    fake = pipeline_client.app.state._fake
    fake.record_workflow_run(REPO, "1_on_pull_request.yml", conclusion="failure")

    _login(pipeline_client, "marvinm2")
    page = pipeline_client.get("/dashboard").text

    assert "could not process this pathway" in page
    assert "See the failing run" in page
    # And it says the portal's own diagram still works, so a curator does not read the failure
    # as "there is nothing to review".
    assert "drawn by this portal and is unaffected" in page


def test_a_successful_pipeline_run_shows_no_warning(pipeline_client):
    _submit(pipeline_client)
    fake = pipeline_client.app.state._fake
    fake.record_workflow_run(REPO, "1_on_pull_request.yml", conclusion="success")

    _login(pipeline_client, "marvinm2")
    page = pipeline_client.get("/dashboard").text

    assert "could not process this pathway" not in page


def test_a_still_running_pipeline_shows_no_warning(pipeline_client):
    # in_progress has conclusion None; treating that as a failure would cry wolf on every
    # submission for the ten minutes the repository takes to process it.
    _submit(pipeline_client)
    fake = pipeline_client.app.state._fake
    fake.record_workflow_run(REPO, "1_on_pull_request.yml", conclusion=None)

    _login(pipeline_client, "marvinm2")
    page = pipeline_client.get("/dashboard").text

    assert "could not process this pathway" not in page
