from __future__ import annotations

import pytest

from app.submit import (
    InvalidGpml,
    assign_wpid,
    layout_paths,
    parse_pathway_meta,
    validate_gpml,
)

# A minimal but realistic GPML2013a header (WPID lives in Version), mirroring WP5636.
GPML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    'Data-Source="WikiPathways" Version="WP5636_r20260520113005" '
    'Organism="Homo sapiens">\n'
    '  <DataNode TextLabel="EIF2AK3"></DataNode>\n'
    "</Pathway>\n"
)

# A malformed upload like audit #94: no Version (=no WPID), still parseable Name/Organism.
GPML_NO_VERSION = (
    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Ketogenesis" '
    'Organism="Homo sapiens"></Pathway>'
)


def test_parse_meta_extracts_embedded_wpid():
    meta = parse_pathway_meta(GPML)
    assert meta.name == "Mitophagy"
    assert meta.organism == "Homo sapiens"
    assert meta.version == "WP5636_r20260520113005"
    assert meta.wpid == "WP5636"


def test_parse_meta_no_version_has_no_wpid():
    meta = parse_pathway_meta(GPML_NO_VERSION)
    assert meta.wpid is None
    assert meta.organism == "Homo sapiens"


def test_validate_rejects_non_gpml():
    with pytest.raises(InvalidGpml) as ei:
        validate_gpml("<html><body>nope</body></html>")
    assert any("Pathway" in r or "GPML" in r for r in ei.value.reasons)


def test_validate_requires_organism():
    no_org = '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="X"></Pathway>'
    with pytest.raises(InvalidGpml) as ei:
        validate_gpml(no_org)
    assert any("Organism" in r for r in ei.value.reasons)


def test_validate_accepts_good_gpml():
    meta = validate_gpml(GPML)
    assert meta.name == "Mitophagy"


def test_assign_wpid_overwrites_existing_version():
    out = assign_wpid(GPML, 5637, revision="r20260724120000")
    assert 'Version="WP5637_r20260724120000"' in out
    assert "WP5636" not in out  # old WPID fully replaced
    # The rest of the document is untouched.
    assert '<DataNode TextLabel="EIF2AK3">' in out
    assert parse_pathway_meta(out).wpid == "WP5637"


def test_assign_wpid_inserts_version_when_absent():
    out = assign_wpid(GPML_NO_VERSION, 5638, revision="r20260724120000")
    assert 'Version="WP5638_r20260724120000"' in out
    assert parse_pathway_meta(out).wpid == "WP5638"


def test_assign_wpid_generates_revision_when_omitted():
    out = assign_wpid(GPML_NO_VERSION, 5639)
    meta = parse_pathway_meta(out)
    assert meta.wpid == "WP5639"
    assert meta.version is not None and meta.version.startswith("WP5639_r")


def test_assign_wpid_fills_missing_author():
    # meta-data-action dereferences Author unconditionally, so a GPML without one crashes the
    # preview's metadata generation. The submitter fills the gap.
    out = assign_wpid(GPML_NO_VERSION, 5640, author="alice")
    assert 'Author="[alice]"' in out


def test_assign_wpid_keeps_existing_author():
    gpml = GPML.replace("<Pathway ", '<Pathway Author="[MadhushriMSV]" ', 1)
    out = assign_wpid(gpml, 5641, author="alice")
    assert 'Author="[MadhushriMSV]"' in out
    assert "alice" not in out


def test_assign_wpid_without_author_leaves_the_tag_alone():
    out = assign_wpid(GPML_NO_VERSION, 5642)
    assert "Author=" not in out


def test_assign_wpid_on_junk_raises():
    with pytest.raises(InvalidGpml):
        assign_wpid("not xml at all", 1)


def test_layout_paths():
    assert layout_paths(5637) == {"gpml": "pathways/WP5637/WP5637.gpml"}
