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


# -- The XML declaration is load-bearing ---------------------------------------------------
#
# `gpml2pvjson` — the converter behind the target repository's `json-svg` job and behind
# `pr-preview.yml`'s validity check — returns zero bytes and **exit status 0** for a GPML whose
# declaration omits `encoding`, or that has none. The job's next step then dies in `JSON.parse`
# with "Unexpected end of JSON input", and the submitter loses their diagram, thumbnail and
# pvjson to a Node stack trace several clicks into the Actions tab.
#
# Found on `marvinm2/sandbox-wp-db` run 30805539734, one of three consecutive new-pathway runs
# that failed this way while updates went green. Reproduced against gpml2pvjson 4.1.8 from both
# directions: stripping `encoding` off a file that converted made it emit nothing, and putting it
# back on the file that failed made it convert. The app passed the declaration through verbatim,
# so it was committing files its own renderer drew quite happily and the pipeline could not read.


def test_assign_wpid_gives_the_file_a_utf8_declaration():
    out = assign_wpid('<?xml version="1.0"?>\n' + GPML_NO_VERSION, 5642)
    assert out.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')
    assert out.count("<?xml") == 1


def test_assign_wpid_adds_a_declaration_to_a_file_with_none():
    out = assign_wpid(GPML_NO_VERSION, 5642)
    assert out.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')
    assert "<Pathway" in out


def test_assign_wpid_leaves_a_correct_declaration_alone():
    out = assign_wpid(GPML, 5642)
    assert out.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')
    assert out.count("<?xml") == 1


def test_a_declared_encoding_is_corrected_rather_than_kept():
    # The app decodes every upload as UTF-8 and commits UTF-8 bytes, so a file that arrived
    # declaring something else is already mislabelled by the time it is written. Rewriting the
    # declaration makes it match the bytes instead of leaving it a lie.
    out = assign_wpid('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + GPML_NO_VERSION, 5642)
    assert "ISO-8859-1" not in out
    assert out.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')


def test_the_declaration_fix_does_not_disturb_the_pathway_body():
    body = GPML.split("?>\n", 1)[1]
    out = assign_wpid('<?xml version="1.0"?>\n' + body, 5642)
    assert '<DataNode TextLabel="EIF2AK3"></DataNode>' in out
    assert out.rstrip().endswith("</Pathway>")
