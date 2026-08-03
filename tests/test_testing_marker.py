"""The contract between the repository's ``testing`` job and the app that reads it.

The workflow cannot be run from here — it belongs to another repository and the staged copy has
deliberately not been proposed yet — so the two halves are pinned against each other instead: the
app's parser against the exact string the workflow builds, and the workflow's own text against the
constants the app keys off.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.review.service import TESTING_CHECKS, TESTING_MARKER, parse_testing_marker

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / "sandbox-workflows"
    / ".github"
    / "workflows"
    / "1_on_pull_request.yml"
)


def _announce_step() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["testing"]["steps"]:
        if step.get("name", "").startswith("Announce the test results"):
            return step
    pytest.fail("the testing job no longer announces its results for machines")


def _comment(**verdicts: str) -> str:
    """What the workflow's ``jq`` produces, in the shape its heredoc wraps it in."""
    payload = json.dumps({"pr": 12, **verdicts}, separators=(",", ":"))
    return f"{TESTING_MARKER}{payload} -->\n\nThe automated tests above, in machine-readable form."


# ---- the workflow's half ----------------------------------------------------------------------


def test_the_workflow_writes_the_marker_the_app_looks_for():
    script = _announce_step()["run"]
    assert TESTING_MARKER.strip() in script, (
        "the workflow's marker and app.review.service.TESTING_MARKER have drifted apart"
    )


def test_the_workflow_reports_every_check_the_app_knows_how_to_read():
    script = _announce_step()["run"]
    for key in TESTING_CHECKS:
        assert f"${key}" in script or f"--arg {key}" in script, (
            f"the workflow no longer reports {key!r}"
        )


def test_the_announcement_never_fails_the_run():
    """It is a convenience for a downstream reader, not part of processing the pathway."""
    assert _announce_step()["continue-on-error"] is True


def test_the_announcement_is_a_comment_and_not_the_pull_request_body():
    """The body is rewritten wholesale on every run, which is the whole reason for the marker."""
    script = _announce_step()["run"]
    assert "issues/$PR_NUMBER/comments" in script
    assert "gh pr edit" not in script


def test_the_announcement_edits_one_comment_rather_than_stacking_them_up():
    script = _announce_step()["run"]
    assert "-X PATCH" in script and "-X POST" in script


# ---- the app's half ----------------------------------------------------------------------------


def test_the_app_reads_the_string_the_workflow_builds():
    verdicts = parse_testing_marker(
        _comment(title="pass", description="review", nodes="review")
    )
    assert verdicts == {"title": "pass", "description": "review", "nodes": "review"}


def test_a_comment_with_no_marker_says_nothing():
    assert parse_testing_marker("Looks good to me!") is None
    assert parse_testing_marker("") is None


def test_a_marker_carrying_broken_json_says_nothing_rather_than_raising():
    assert parse_testing_marker(f"{TESTING_MARKER}{{not json}} -->") is None


def test_an_unknown_field_cannot_reach_a_template():
    """The repository is free to add keys; only the three it is read for are kept."""
    body = f'{TESTING_MARKER}{{"pr":1,"title":"pass","surprise":"<script>"}} -->'
    assert parse_testing_marker(body) == {"title": "pass"}


def test_a_marker_with_no_known_verdicts_reads_as_nothing():
    assert parse_testing_marker(f'{TESTING_MARKER}{{"pr":1}} -->') is None
