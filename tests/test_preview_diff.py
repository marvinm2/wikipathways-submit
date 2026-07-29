"""What changed between an update's two renders (issue #24)."""
from __future__ import annotations

from app.preview.diff import diff_nodes
from app.preview.render import render_gpml_with_nodes


def node(**over):
    base = {
        "label": "AKT1",
        "type": "GeneProduct",
        "database": "Ensembl",
        "identifier": "ENSG00000142208",
        "graph_id": "a1",
        "cx": 100.0,
        "cy": 100.0,
        "left": 1.0, "top": 1.0, "width": 1.0, "height": 1.0, "url": None, "comment": "",
    }
    base.update(over)
    return base


def test_an_unchanged_pathway_reports_nothing_changed():
    nodes = [node(), node(label="TP53", identifier="ENSG00000141510", graph_id="b2")]
    d = diff_nodes(nodes, [dict(n) for n in nodes])
    assert d.summary["unchanged"] == 2
    assert [d.summary[k] for k in ("added", "removed", "reannotated", "relabelled", "moved")] == [
        0, 0, 0, 0, 0
    ]


def test_reannotation_is_caught_even_though_the_box_looks_identical():
    # The category the issue calls the most review-relevant and the least visible: same label,
    # same place, a different thing behind it.
    before = [node(identifier="ENSG00000000000")]
    after = [node(identifier="ENSG00000142208")]
    d = diff_nodes(before, after)
    assert d.summary["reannotated"] == 1
    assert d.after[0]["change"] == "reannotated"
    # The panel strikes the old value through, so it has to be carried.
    assert d.after[0]["was"] == {
        "label": "AKT1", "database": "Ensembl", "identifier": "ENSG00000000000",
    }


def test_added_and_removed():
    before = [node()]
    after = [node(), node(label="TP53", identifier="ENSG00000141510", graph_id="c3")]
    d = diff_nodes(before, after)
    assert d.summary["added"] == 1 and d.summary["removed"] == 0
    assert d.after[1]["change"] == "added"

    d2 = diff_nodes(after, before)
    assert d2.summary["removed"] == 1
    assert d2.before[1]["change"] == "removed"


def test_a_node_matches_by_label_when_the_graph_id_was_rewritten():
    # PathVisio does not always preserve GraphId, and an identity that depended on it would
    # report a whole re-saved pathway as deleted and re-added.
    before = [node(graph_id="old")]
    after = [node(graph_id="brand-new")]
    assert diff_nodes(before, after).summary["unchanged"] == 1


def test_a_relabelled_node_is_matched_on_its_annotation_not_lost():
    before = [node(label="AKT", graph_id="")]
    after = [node(label="AKT1 kinase", graph_id="")]
    d = diff_nodes(before, after)
    assert d.summary["relabelled"] == 1
    assert d.after[0]["was"]["label"] == "AKT"


def test_label_and_annotation_both_changed_is_a_delete_plus_an_add():
    # Nothing links these two; claiming they are the same node would be an invention.
    before = [node(label="AKT", identifier="ENSG00000000000", graph_id="")]
    after = [node(label="TP53", identifier="ENSG00000141510", graph_id="")]
    d = diff_nodes(before, after)
    assert d.summary["added"] == 1 and d.summary["removed"] == 1


def test_unannotated_nodes_do_not_all_collide_on_the_empty_xref():
    # Every unannotated node shares ("", ""). Matching on that would pair arbitrary boxes, so the
    # xref pass skips them and they fall back to label + type.
    before = [node(label="one", database="", identifier="", graph_id=""),
              node(label="two", database="", identifier="", graph_id="")]
    after = [node(label="two", database="", identifier="", graph_id=""),
             node(label="three", database="", identifier="", graph_id="")]
    d = diff_nodes(before, after)
    assert d.summary["unchanged"] == 1   # "two"
    assert d.summary["added"] == 1       # "three"
    assert d.summary["removed"] == 1     # "one"


def test_a_move_is_measured_in_user_units_not_board_percentages():
    # A submitter who widens BoardWidth changes every percentage without moving anything on the
    # diagram. Percentages differ wildly here and the centre does not: it did not move.
    before = [node(left=10.0, top=10.0)]
    after = [node(left=80.0, top=90.0)]
    assert diff_nodes(before, after).summary["moved"] == 0

    moved = diff_nodes(before, [node(cx=400.0)])
    assert moved.summary["moved"] == 1
    assert moved.after[0]["change"] == "moved"


def test_a_cache_without_centres_reports_no_movement_rather_than_guessing():
    # Renders cached before this shipped carry no cx/cy. Reading movement out of the percentages
    # would be wrong on any board change, so those nodes simply say nothing about position.
    before = [node(cx=None, cy=None, left=5.0)]
    after = [node(cx=None, cy=None, left=60.0)]
    assert diff_nodes(before, after).summary["moved"] == 0


def test_reannotation_outranks_a_move():
    # Both are true; the curator needs told about the one they cannot see.
    d = diff_nodes([node()], [node(identifier="ENSG00000141510", cx=500.0)])
    assert d.after[0]["change"] == "reannotated"


def test_duplicate_labels_pair_up_instead_of_reading_as_wholesale_churn():
    before = [node(label="AKT1", graph_id=""), node(label="AKT1", graph_id="")]
    after = [node(label="AKT1", graph_id=""), node(label="AKT1", graph_id="")]
    d = diff_nodes(before, after)
    assert d.summary["unchanged"] == 2
    assert d.summary["added"] == 0 and d.summary["removed"] == 0


def test_the_arrays_stay_index_aligned_with_each_side():
    # The overlay colours hotspot i from entry i. A length that disagrees with the nodes file is
    # the one failure the client cannot detect from the content, so it must not happen here.
    before = [node(graph_id="x"), node(label="gone", graph_id="y")]
    after = [node(graph_id="x"), node(label="new", graph_id="z"), node(label="also", graph_id="w")]
    d = diff_nodes(before, after)
    assert len(d.before) == len(before)
    assert len(d.after) == len(after)


def test_end_to_end_from_real_gpml():
    # The diff consumes what render_gpml_with_nodes writes, so the two have to agree on the field
    # names — a rename in the renderer would otherwise silently classify everything as changed.
    def gpml(nodes_xml):
        return (
            '<?xml version="1.0"?><Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="P">'
            '<Graphics BoardWidth="400" BoardHeight="300"/>' + nodes_xml + "</Pathway>"
        )

    before_xml = (
        '<DataNode TextLabel="AKT1" Type="GeneProduct" GraphId="n1">'
        '<Graphics CenterX="100" CenterY="100" Width="80" Height="20"/>'
        '<Xref Database="Ensembl" ID="ENSG00000000000"/></DataNode>'
        '<DataNode TextLabel="Old" Type="GeneProduct" GraphId="n2">'
        '<Graphics CenterX="100" CenterY="200" Width="80" Height="20"/>'
        '<Xref Database="Ensembl" ID="ENSG00000111111"/></DataNode>'
    )
    after_xml = (
        '<DataNode TextLabel="AKT1" Type="GeneProduct" GraphId="n1">'
        '<Graphics CenterX="100" CenterY="100" Width="80" Height="20"/>'
        '<Xref Database="Ensembl" ID="ENSG00000142208"/></DataNode>'
        '<DataNode TextLabel="New" Type="GeneProduct" GraphId="n3">'
        '<Graphics CenterX="250" CenterY="200" Width="80" Height="20"/>'
        '<Xref Database="Ensembl" ID="ENSG00000222222"/></DataNode>'
    )
    _, b = render_gpml_with_nodes(gpml(before_xml))
    _, a = render_gpml_with_nodes(gpml(after_xml))
    d = diff_nodes([h.as_dict() for h in b], [h.as_dict() for h in a])
    assert d.summary["reannotated"] == 1
    assert d.summary["added"] == 1
    assert d.summary["removed"] == 1
    assert d.summary["unchanged"] == 0
