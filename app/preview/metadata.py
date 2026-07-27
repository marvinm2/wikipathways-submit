"""Parse the curation-facing metadata out of a GPML pathway.

The review dashboard shows more than a picture: the data nodes and their identifiers, the
literature references, the pathway description, and the ontology tags. All of it lives in the
GPML the submitter uploaded, so we parse it once (at preview-render time) and cache it next to
the rendered SVGs, keeping the dashboard render a cheap disk read.

Parsing is namespace-tolerant (GPML uses a default namespace; the embedded Biopax block uses the
biopax-level3 namespace) via local-name matching, the same approach as ``app.preview.render``.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_child(el: ET.Element, name: str) -> ET.Element | None:
    for child in el:
        if _localname(child.tag) == name:
            return child
    return None


@dataclass(frozen=True)
class DataNode:
    label: str
    type: str
    database: str
    identifier: str


@dataclass(frozen=True)
class Reference:
    identifier: str
    database: str
    title: str


@dataclass(frozen=True)
class OntologyTag:
    term: str
    identifier: str
    ontology: str


@dataclass(frozen=True)
class CurationMetadata:
    name: str | None = None
    organism: str | None = None
    description: str | None = None
    data_nodes: list[DataNode] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    ontology_tags: list[OntologyTag] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_curation_metadata(gpml: bytes | str) -> CurationMetadata:
    """Extract the review-panel metadata from a GPML document. Never raises on malformed input —
    returns whatever could be parsed (an empty metadata object for non-GPML)."""
    text = gpml.decode("utf-8", "replace") if isinstance(gpml, bytes) else gpml
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return CurationMetadata()
    if _localname(root.tag) != "Pathway":
        return CurationMetadata()

    name = root.get("Name")
    organism = root.get("Organism")

    data_nodes: list[DataNode] = []
    references: list[Reference] = []
    ontology_tags: list[OntologyTag] = []
    comments: list[str] = []

    for el in root.iter():
        kind = _localname(el.tag)
        if kind == "DataNode":
            xref = _find_child(el, "Xref")
            data_nodes.append(
                DataNode(
                    label=(el.get("TextLabel") or "").strip(),
                    type=el.get("Type") or "Unknown",
                    database=(xref.get("Database") if xref is not None else "") or "",
                    identifier=(xref.get("ID") if xref is not None else "") or "",
                )
            )
        elif kind == "OntologyTerm":
            ontology_tags.append(
                OntologyTag(
                    term=(el.get("Term") or "").strip(),
                    identifier=(el.get("ID") or "").strip(),
                    ontology=(el.get("Ontology") or "").strip(),
                )
            )
        elif kind == "PublicationXref":
            # Biopax literature reference: bp:ID / bp:DB / bp:TITLE children.
            references.append(
                Reference(
                    identifier=_text(_find_child(el, "ID")),
                    database=_text(_find_child(el, "DB")),
                    title=_text(_find_child(el, "TITLE")),
                )
            )

    # Pathway description lives in top-level <Comment> children (skip nested ones on elements).
    for child in root:
        if _localname(child.tag) == "Comment" and (child.text or "").strip():
            comments.append(child.text.strip())

    description = "\n\n".join(comments) or None
    return CurationMetadata(
        name=name,
        organism=organism,
        description=description,
        data_nodes=data_nodes,
        references=references,
        ontology_tags=ontology_tags,
    )
