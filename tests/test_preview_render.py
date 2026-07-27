"""In-app GPML -> SVG preview (issue #11): the renderer and the cache it serves from."""
from __future__ import annotations

import pytest

from app.github import FakeGitHubClient
from app.preview import PreviewService
from app.preview.render import RenderError, render_gpml

_GPML = """<?xml version="1.0" encoding="UTF-8"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Demo" Organism="Homo sapiens">
  <Graphics BoardWidth="400.0" BoardHeight="300.0" />
  <DataNode TextLabel="TP53" Type="GeneProduct">
    <Graphics CenterX="100.0" CenterY="100.0" Width="80.0" Height="24.0" />
    <Xref Database="Ensembl" ID="ENSG00000141510" />
  </DataNode>
  <DataNode TextLabel="MDM2" Type="GeneProduct">
    <Graphics CenterX="300.0" CenterY="100.0" Width="80.0" Height="24.0" />
    <Xref Database="Ensembl" ID="ENSG00000135679" />
  </DataNode>
  <Interaction>
    <Graphics>
      <Point X="140.0" Y="100.0" />
      <Point X="260.0" Y="100.0" ArrowHead="mim-stimulation" />
    </Graphics>
  </Interaction>
</Pathway>
"""


def test_render_produces_valid_svg_with_nodes_edges_labels():
    svg = render_gpml(_GPML.encode()).decode()
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert 'viewBox="0.0 0.0 400.0 300.0"' in svg  # honours the declared board
    assert svg.count("<rect") == 3  # board background + 2 data nodes
    assert "TP53" in svg and "MDM2" in svg  # labels
    assert "<polyline" in svg and "marker-end" in svg  # interaction with an arrowhead


def test_render_infers_viewbox_when_board_missing():
    gpml = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="x" Organism="Homo sapiens">'
        '<DataNode TextLabel="A"><Graphics CenterX="50" CenterY="50" Width="20" Height="20"/>'
        "</DataNode></Pathway>"
    )
    svg = render_gpml(gpml).decode()
    # bbox is 40..60 in both axes, padded by 15 → origin 25,25 size 50x50
    assert 'viewBox="25.0 25.0 50.0 50.0"' in svg


def test_render_escapes_label_text():
    gpml = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="x" Organism="Homo sapiens">'
        '<DataNode TextLabel="A &amp; B &lt;x&gt;"><Graphics CenterX="10" CenterY="10" '
        'Width="10" Height="10"/></DataNode></Pathway>'
    )
    svg = render_gpml(gpml).decode()
    assert "A &amp; B &lt;x&gt;" in svg  # not raw & or <


@pytest.mark.parametrize("bad", [b"", b"<not-gpml/>", b"garbage", b"<Pathway>unterminated"])
def test_render_raises_on_non_gpml(bad):
    with pytest.raises(RenderError):
        render_gpml(bad)


def _svc(tmp_path):
    return PreviewService(cache_dir=tmp_path / "cache")


def test_render_local_serves_both_sides(tmp_path):
    svc = _svc(tmp_path)
    state = svc.render_local(42, 554, after_gpml=_GPML.encode(), before_gpml=_GPML.encode())
    assert state.status == "ready" and state.has_before and state.has_after
    assert svc.status(42) == "ready"
    assert svc.svg_path(42, "after") is not None
    assert svc.svg_path(42, "before") is not None
    assert b"<svg" in (tmp_path / "cache" / "42" / "after.svg").read_bytes()


def test_render_local_new_pathway_has_no_before(tmp_path):
    svc = _svc(tmp_path)
    state = svc.render_local(7, 5640, after_gpml=_GPML.encode())  # no before_gpml
    assert state.status == "ready" and state.has_after and not state.has_before
    assert svc.svg_path(7, "after") is not None
    assert svc.svg_path(7, "before") is None  # absent side → placeholder


def test_render_local_bad_gpml_fails_gracefully(tmp_path):
    svc = _svc(tmp_path)
    state = svc.render_local(9, 1, after_gpml=b"not gpml at all")
    assert state.status == "failed" and not state.has_after
    # ...and the queue says so, instead of spinning on "generating" forever.
    assert svc.status(9) == "failed"


def test_re_render_clears_a_previous_failure(tmp_path):
    svc = _svc(tmp_path)
    svc.render_local(9, 1, after_gpml=b"not gpml at all")
    assert svc.status(9) == "failed"
    svc.render_local(9, 1, after_gpml=_GPML.encode())  # revised upload draws fine
    assert svc.status(9) == "ready"


def test_status_pending_before_anything_is_rendered(tmp_path):
    assert _svc(tmp_path).status(123) == "pending"


def test_render_local_caches_metadata_and_note(tmp_path):
    svc = _svc(tmp_path)
    svc.render_local(
        42, 554, after_gpml=_GPML.encode(), submitter_note="  please check the arrow  "
    )
    meta = svc.metadata(42)
    assert meta is not None
    assert [n["label"] for n in meta["data_nodes"]] == ["TP53", "MDM2"]
    assert meta["submitter_note"] == "please check the arrow"  # trimmed
    assert svc.metadata(999) is None  # nothing rendered for this PR


def test_get_file_content_roundtrip():
    fake = FakeGitHubClient(existing_contents={"owner/repo#pathways/WP554/WP554.gpml": _GPML})
    got = fake.get_file_content("owner/repo", "main", "pathways/WP554/WP554.gpml")
    assert got is not None and b"<Pathway" in got
    assert fake.get_file_content("owner/repo", "main", "pathways/WP999/WP999.gpml") is None
