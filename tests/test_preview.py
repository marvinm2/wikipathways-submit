"""Pathway preview (issue #11): artifact consumption + before/after SVG serving."""
from __future__ import annotations

import io
import zipfile

from app.github import FakeGitHubClient
from app.preview import PreviewService


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _svc(fake, tmp_path):
    return PreviewService(
        lambda: fake,
        repo="wikipathways/wikipathways-database",
        cache_dir=tmp_path / "cache",
        workflow_file="pr-preview.yml",
        artifact_name="pr-preview",
    )


def test_extracts_before_and_after(tmp_path):
    z = _zip(
        {
            "WP4255/WP4255.svg": b"<svg>after</svg>",
            "WP4255/WP4255-before.svg": b"<svg>before</svg>",
            "status.txt": b"pass",
        }
    )
    svc = _svc(FakeGitHubClient(previews={7: {"status": "ready", "zip": z}}), tmp_path)
    assert svc.status(7) == "ready"
    state = svc.ensure(7, 4255)
    assert state.status == "ready" and state.has_before and state.has_after
    assert svc.svg_path(7, 4255, "after").read_bytes() == b"<svg>after</svg>"
    assert svc.svg_path(7, 4255, "before").read_bytes() == b"<svg>before</svg>"


def test_new_pathway_has_after_only(tmp_path):
    z = _zip({"WP5637/WP5637.svg": b"<svg>new</svg>"})
    svc = _svc(FakeGitHubClient(previews={8: {"status": "ready", "zip": z}}), tmp_path)
    state = svc.ensure(8, 5637)
    assert state.has_after and not state.has_before
    assert svc.svg_path(8, 5637, "before") is None  # → route serves the placeholder


def test_prefers_dedicated_after_over_gpmlconverter(tmp_path):
    # When both WP<id>-after.svg (PinPath) and WP<id>.svg (gpmlconverter) exist, prefer -after.
    z = _zip(
        {
            "WP4255/WP4255.svg": b"<svg>gpmlconverter</svg>",
            "WP4255/WP4255-after.svg": b"<svg>pinpath</svg>",
            "WP4255/WP4255-before.svg": b"<svg>pinpath-before</svg>",
        }
    )
    svc = _svc(FakeGitHubClient(previews={7: {"status": "ready", "zip": z}}), tmp_path)
    svc.ensure(7, 4255)
    assert svc.svg_path(7, 4255, "after").read_bytes() == b"<svg>pinpath</svg>"


def test_falls_back_to_gpmlconverter_after(tmp_path):
    z = _zip({"WP4255/WP4255.svg": b"<svg>gpmlconverter</svg>"})
    svc = _svc(FakeGitHubClient(previews={7: {"status": "ready", "zip": z}}), tmp_path)
    svc.ensure(7, 4255)
    assert svc.svg_path(7, 4255, "after").read_bytes() == b"<svg>gpmlconverter</svg>"


def test_status_pending_failed_absent(tmp_path):
    fake = FakeGitHubClient(previews={1: {"status": "pending"}, 2: {"status": "failed"}})
    svc = _svc(fake, tmp_path)
    assert svc.status(1) == "pending"
    assert svc.status(2) == "failed"
    assert svc.status(99) == "pending"  # absent PR reads as pending to the UI


def test_no_bot_stays_pending(tmp_path):
    svc = PreviewService(
        lambda: None, repo="o/r", cache_dir=tmp_path, workflow_file="w", artifact_name="a"
    )
    assert svc.status(1) == "pending"
    assert svc.ensure(1, 1).status == "pending"


def test_bad_zip_is_failed(tmp_path):
    svc = _svc(FakeGitHubClient(previews={1: {"status": "ready", "zip": b"not a zip"}}), tmp_path)
    assert svc.ensure(1, 1).status == "failed"


def test_empty_artifact_is_failed(tmp_path):
    # Ready run but the zip has no matching SVGs for this wpid.
    z = _zip({"status.txt": b"pass", "WP9999/WP9999.svg": b"<svg/>"})
    svc = _svc(FakeGitHubClient(previews={1: {"status": "ready", "zip": z}}), tmp_path)
    assert svc.ensure(1, 4255).status == "failed"


def test_status_is_cached(tmp_path):
    fake = FakeGitHubClient(previews={1: {"status": "pending"}})
    svc = _svc(fake, tmp_path)
    assert svc.status(1) == "pending"
    fake.previews[1]["status"] = "ready"  # would change, but within TTL the cache holds
    assert svc.status(1) == "pending"
