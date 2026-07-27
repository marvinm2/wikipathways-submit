from __future__ import annotations

from app.preview.metadata import parse_curation_metadata
from app.review.checklist import build_checklist, default_checklist, is_complete

# Two mapped data nodes, a title + description, an ontology tag, no references.
MAPPED = """<?xml version="1.0"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Demo" Organism="Homo sapiens">
  <Comment>A demo pathway.</Comment>
  <DataNode TextLabel="INSR" Type="GeneProduct">
    <Xref Database="Ensembl" ID="ENSG00000171105"/></DataNode>
  <DataNode TextLabel="AKT1" Type="GeneProduct">
    <Xref Database="Ensembl" ID="ENSG00000142208"/></DataNode>
  <OntologyTerm Term="insulin signaling pathway" ID="PW:0000143" Ontology="Pathway Ontology"/>
</Pathway>
"""

# One node with no identifier.
UNMAPPED = """<?xml version="1.0"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Demo" Organism="Homo sapiens">
  <DataNode TextLabel="MysteryGene" Type="GeneProduct"><Xref Database="" ID=""/></DataNode>
</Pathway>
"""


def _item(checklist, key):
    return next(i for i in checklist if i["key"] == key)


def test_auto_checks_prefill_states():
    cl = build_checklist(metadata=parse_curation_metadata(MAPPED), kind="new")
    assert _item(cl, "datanodes_mapped")["state"] == "pass"
    assert _item(cl, "datanodes_mapped")["auto"] is True
    assert _item(cl, "naming_ok")["state"] == "pass"
    assert _item(cl, "ontology_tags")["state"] == "pass"
    # "Title and description are meaningful" is a human judgement — no auto state.
    assert _item(cl, "description_ok")["state"] == "pending"
    assert _item(cl, "description_ok")["auto"] is False
    refs = _item(cl, "references_valid")
    assert refs["state"] == "na"  # no references
    assert refs["required"] is False  # auto-N/A must not block approval
    # The render check stays a human judgement — no auto state.
    assert _item(cl, "render_ok")["state"] == "pending"
    assert _item(cl, "render_ok")["auto"] is False


def test_well_annotated_submission_only_needs_human_checks():
    cl = build_checklist(metadata=parse_curation_metadata(MAPPED), kind="new")
    assert is_complete(cl) is False  # render_ok + description_ok still pending
    # The structural checks auto-pass; only the two human judgements remain.
    for key in ("render_ok", "description_ok"):
        assert _item(cl, key)["state"] == "pending"
        _item(cl, key)["state"] = "pass"
    assert is_complete(cl) is True


def test_unmapped_datanode_auto_fails_with_note():
    cl = build_checklist(metadata=parse_curation_metadata(UNMAPPED), kind="new")
    item = _item(cl, "datanodes_mapped")
    assert item["state"] == "fail"
    assert "no identifier" in item["note"]
    assert "MysteryGene" in item["note"]


def test_default_checklist_is_all_pending():
    cl = default_checklist()
    assert all(i["state"] == "pending" and i["auto"] is False for i in cl)


def test_update_scopes_unchanged_checks_to_na():
    before = parse_curation_metadata(MAPPED)
    after = parse_curation_metadata(MAPPED)  # identical → nothing changed
    cl = build_checklist(metadata=after, before=before, kind="update")
    dn = _item(cl, "datanodes_mapped")
    assert dn["state"] == "na"
    assert dn["required"] is False  # irrelevant → non-blocking
    assert "Not relevant" in dn["note"]
    # A check with no relevance rule (render) is always kept.
    assert _item(cl, "render_ok")["required"] is True
    # With every changeable subject unchanged and render marked pass, approval isn't blocked by
    # the scoped-out items.
    for i in cl:
        if i["key"] == "render_ok":
            i["state"] = "pass"
    assert is_complete(cl) is True


def test_update_keeps_check_when_subject_changed():
    before = parse_curation_metadata(MAPPED)
    after = parse_curation_metadata(UNMAPPED)  # data nodes differ
    cl = build_checklist(metadata=after, before=before, kind="update")
    dn = _item(cl, "datanodes_mapped")
    assert dn["required"] is True  # relevant → normal auto-check runs
    assert dn["state"] == "fail"
