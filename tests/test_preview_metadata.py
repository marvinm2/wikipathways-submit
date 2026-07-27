from __future__ import annotations

from app.preview.metadata import parse_curation_metadata

GPML = """<?xml version="1.0" encoding="UTF-8"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a"
         Name="Insulin signaling (demo)" Organism="Homo sapiens">
  <Comment>Insulin signaling demo pathway for the curation portal.</Comment>
  <DataNode TextLabel="INSR" Type="GeneProduct">
    <Graphics CenterX="240" CenterY="200" Width="120" Height="34"/>
    <Xref Database="Ensembl" ID="ENSG00000171105"/>
  </DataNode>
  <DataNode TextLabel="MysteryProtein" Type="GeneProduct">
    <Graphics CenterX="240" CenterY="320" Width="120" Height="34"/>
    <Xref Database="" ID=""/>
  </DataNode>
  <OntologyTerm Term="insulin signaling pathway" ID="PW:0000143" Ontology="Pathway Ontology"/>
  <Biopax>
    <bp:PublicationXref xmlns:bp="http://www.biopax.org/release/biopax-level3.owl#"
                        xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" rdf:id="ref1">
      <bp:ID>17635937</bp:ID>
      <bp:DB>PubMed</bp:DB>
      <bp:TITLE>A paper about insulin signaling</bp:TITLE>
    </bp:PublicationXref>
  </Biopax>
</Pathway>
"""


def test_parses_all_sections():
    m = parse_curation_metadata(GPML)
    assert m.name == "Insulin signaling (demo)"
    assert m.organism == "Homo sapiens"
    assert m.description == "Insulin signaling demo pathway for the curation portal."

    assert [n.label for n in m.data_nodes] == ["INSR", "MysteryProtein"]
    insr = m.data_nodes[0]
    assert insr.database == "Ensembl"
    assert insr.identifier == "ENSG00000171105"
    assert insr.type == "GeneProduct"
    # Mapped nodes get a resolvable identifiers.org link; unmapped ones do not.
    assert insr.url == "https://identifiers.org/ensembl:ENSG00000171105"
    assert m.data_nodes[1].url is None
    assert m.references[0].url == "https://identifiers.org/pubmed:17635937"
    # An unannotated node keeps empty database/identifier so the UI can flag it.
    assert m.data_nodes[1].identifier == ""

    assert len(m.references) == 1
    assert m.references[0].identifier == "17635937"
    assert m.references[0].database == "PubMed"
    assert "insulin signaling" in m.references[0].title

    assert len(m.ontology_tags) == 1
    assert m.ontology_tags[0].identifier == "PW:0000143"
    assert m.ontology_tags[0].ontology == "Pathway Ontology"


def test_non_gpml_returns_empty_metadata():
    m = parse_curation_metadata("<html>not gpml</html>")
    assert m.name is None and m.data_nodes == [] and m.references == []
    assert m.as_dict()["data_nodes"] == []


def test_malformed_xml_does_not_raise():
    m = parse_curation_metadata("<Pathway><unclosed>")
    assert m.data_nodes == []
