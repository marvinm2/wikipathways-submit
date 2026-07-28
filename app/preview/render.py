"""Instant, dependency-free GPML -> SVG renderer for the before/after preview (issue #11, 1a).

The PR-preview *workflow* still produces the canonical artifacts (metadata, datanodes, refs,
validation, and the ``WP<id>.json`` pvjson), but the human-facing preview no longer waits on CI:
the app has the submitted GPML bytes at PR-creation time, so it renders the "after" here and the
"before" from the base-branch GPML, instantly. GPML carries explicit coordinates
(``BoardWidth``/``Height``, ``CenterX``/``CenterY``/``Width``/``Height``, interaction ``Point``s),
so a faithful-enough diagram is a direct draw — no R, no PinPath, no external service.

This is deliberately a *review* render (see what changed), not a pixel-perfect WikiPathways
render; the clickable pvjs viewer (issue #14) is the high-fidelity path. ``render_gpml`` never
raises on a malformed *element* (it's skipped); it raises ``RenderError`` only when the input
isn't parseable GPML at all, so a bad upload degrades to the placeholder rather than a 500.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from xml.sax.saxutils import escape

from app.preview.metadata import resolver_url

_PAD = 15.0  # board padding when we must infer the viewBox from element extents

# Light type-based accents (fill stays near-white like WikiPathways; the accent is the border/text).
_TYPE_STROKE = {
    "GeneProduct": "#2166ac",
    "Protein": "#2166ac",
    "Rna": "#2166ac",
    "Metabolite": "#1a9850",
    "Pathway": "#762a83",
}
_DEFAULT_STROKE = "#333333"


class RenderError(ValueError):
    """The input could not be parsed as GPML."""


@dataclass(frozen=True)
class NodeHotspot:
    """One clickable data node: where it sits on the drawing, and what to say about it.

    Geometry is a **percentage of the viewBox**, not user units, so the overlay lines up at any
    size without the client knowing the coordinate system. The drawing is served as an ``<img>``
    (which keeps a hostile GPML's render inert), so the hotspots cannot live inside the SVG —
    they are laid over it, and percentages are what survives the viewport's resize-based zoom.

    Built in the same pass that draws the node, from the same element. An earlier sketch joined
    ``render`` geometry to ``metadata`` properties by list index; that only holds while both
    passes skip exactly the same elements, and they do not — the renderer drops a DataNode with
    no ``Graphics``, the metadata parser keeps it.
    """

    left: float
    top: float
    width: float
    height: float
    label: str
    type: str
    database: str
    identifier: str
    url: str | None
    comment: str

    def as_dict(self) -> dict:
        return asdict(self)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _color(raw: str | None) -> str | None:
    """GPML colours are hex RRGGBB without a leading '#'; 'Transparent' means no paint."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.lower() in ("transparent", "none"):
        return None
    if re.fullmatch(r"[0-9A-Fa-f]{6}", raw):
        return "#" + raw
    return raw  # a CSS colour name, passed through


def _f(v: str | None) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _graphics(el: ET.Element) -> ET.Element | None:
    for child in el:
        if _localname(child.tag) == "Graphics":
            return child
    return None


def _rect_of(g: ET.Element) -> tuple[float, float, float, float] | None:
    cx, cy = _f(g.get("CenterX")), _f(g.get("CenterY"))
    w, h = _f(g.get("Width")), _f(g.get("Height"))
    if None in (cx, cy, w, h):
        return None
    return cx - w / 2, cy - h / 2, w, h  # type: ignore[operator]


def render_gpml(gpml: bytes | str) -> bytes:
    """Render a GPML pathway to a standalone SVG (bytes). Raises RenderError on non-GPML input."""
    return render_gpml_with_nodes(gpml)[0]


def render_gpml_with_nodes(gpml: bytes | str) -> tuple[bytes, list[NodeHotspot]]:
    """Render to SVG *and* return the clickable data-node hotspots (issue #14).

    One pass, so a hotspot cannot drift from the rectangle it covers.
    """
    text = gpml.decode("utf-8", "replace") if isinstance(gpml, bytes) else gpml
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RenderError(f"not parseable XML: {exc}") from exc
    if _localname(root.tag) != "Pathway":
        raise RenderError("root element is not <Pathway>")

    nodes: list[str] = []
    edges: list[str] = []
    xs: list[float] = []
    ys: list[float] = []
    # (x, y, w, h) in user units plus the node's properties. Converted to viewBox percentages
    # once the viewBox is known, which is only after the loop has seen every element.
    raw_hotspots: list[tuple[tuple[float, float, float, float], dict]] = []

    def track(x: float, y: float) -> None:
        xs.append(x)
        ys.append(y)

    for el in root.iter():
        kind = _localname(el.tag)

        if kind in ("DataNode", "Label", "Shape"):
            g = _graphics(el)
            if g is None:
                continue
            rect = _rect_of(g)
            if rect is None:
                continue
            x, y, w, h = rect
            track(x, y)
            track(x + w, y + h)
            stroke = _color(g.get("Color")) or (
                _TYPE_STROKE.get(el.get("Type", "")) if kind == "DataNode" else None
            ) or (_DEFAULT_STROKE if kind != "Label" else "none")
            fill = _color(g.get("FillColor")) or ("#ffffff" if kind != "Label" else "none")
            rx = 8 if (kind == "DataNode" and el.get("Type") == "Metabolite") else 2
            if kind != "Label":
                nodes.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                    f'rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="1"/>'
                )
            if kind == "DataNode":
                xref = next((c for c in el if _localname(c.tag) == "Xref"), None)
                database = (xref.get("Database") if xref is not None else "") or ""
                identifier = (xref.get("ID") if xref is not None else "") or ""
                # A DataNode can carry several <Comment>s; the curator wants all of them, and
                # joining beats picking one arbitrarily.
                comment = "\n\n".join(
                    (c.text or "").strip()
                    for c in el
                    if _localname(c.tag) == "Comment" and (c.text or "").strip()
                )
                raw_hotspots.append(
                    (
                        (x, y, w, h),
                        {
                            "label": (el.get("TextLabel") or "").strip(),
                            "type": el.get("Type") or "Unknown",
                            "database": database.strip(),
                            "identifier": identifier.strip(),
                            "url": resolver_url(database, identifier),
                            "comment": comment,
                        },
                    )
                )

            label = el.get("TextLabel")
            if label:
                nodes.append(
                    f'<text x="{x + w / 2:.1f}" y="{y + h / 2:.1f}" text-anchor="middle" '
                    f'dominant-baseline="central" font-family="Arial, sans-serif" '
                    f'font-size="10" fill="{stroke if stroke != "none" else _DEFAULT_STROKE}">'
                    f"{escape(label)}</text>"
                )

        elif kind in ("Interaction", "GraphicalLine"):
            g = _graphics(el)
            if g is None:
                continue
            pts = [
                (_f(p.get("X")), _f(p.get("Y")), p.get("ArrowHead"))
                for p in g
                if _localname(p.tag) == "Point"
            ]
            pts = [(x, y, a) for (x, y, a) in pts if x is not None and y is not None]
            if len(pts) < 2:
                continue
            for x, y, _ in pts:
                track(x, y)  # type: ignore[arg-type]
            poly = " ".join(f"{x:.1f},{y:.1f}" for (x, y, _) in pts)
            arrow = pts[-1][2]
            marker = ' marker-end="url(#arrow)"' if arrow and arrow.lower() != "line" else ""
            edges.append(
                f'<polyline points="{poly}" fill="none" stroke="{_DEFAULT_STROKE}" '
                f'stroke-width="1"{marker}/>'
            )

    # viewBox: prefer the declared board, else the element extents with padding.
    pg = _graphics(root)
    bw = _f(pg.get("BoardWidth")) if pg is not None else None
    bh = _f(pg.get("BoardHeight")) if pg is not None else None
    if bw and bh:
        vb_x, vb_y, vb_w, vb_h = 0.0, 0.0, bw, bh
    elif xs and ys:
        vb_x, vb_y = min(xs) - _PAD, min(ys) - _PAD
        vb_w, vb_h = (max(xs) - min(xs)) + 2 * _PAD, (max(ys) - min(ys)) + 2 * _PAD
    else:
        vb_x, vb_y, vb_w, vb_h = 0.0, 0.0, 100.0, 100.0

    body = "\n".join(edges + nodes)  # edges under nodes
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{vb_x:.1f} {vb_y:.1f} {vb_w:.1f} {vb_h:.1f}" '
        f'width="{vb_w:.0f}" height="{vb_h:.0f}">\n'
        f'<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" '
        f'orient="auto" markerUnits="strokeWidth">'
        f'<path d="M0,0 L7,3 L0,6 z" fill="{_DEFAULT_STROKE}"/></marker></defs>\n'
        f'<rect x="{vb_x:.1f}" y="{vb_y:.1f}" width="{vb_w:.1f}" height="{vb_h:.1f}" '
        f'fill="#ffffff"/>\n'
        f"{body}\n</svg>\n"
    )

    # User units -> percentage of the viewBox. A zero-sized viewBox would divide by zero, and a
    # node sitting outside a declared BoardWidth/Height would land off the image, so clamp both.
    hotspots: list[NodeHotspot] = []
    if vb_w > 0 and vb_h > 0:
        for (x, y, w, h), props in raw_hotspots:
            left = (x - vb_x) / vb_w * 100.0
            top = (y - vb_y) / vb_h * 100.0
            width = w / vb_w * 100.0
            height = h / vb_h * 100.0
            if left + width <= 0 or top + height <= 0 or left >= 100 or top >= 100:
                continue  # entirely off the drawing; a hotspot there is unclickable anyway
            hotspots.append(
                NodeHotspot(
                    left=round(max(0.0, left), 3),
                    top=round(max(0.0, top), 3),
                    width=round(min(width, 100.0 - max(0.0, left)), 3),
                    height=round(min(height, 100.0 - max(0.0, top)), 3),
                    **props,
                )
            )
    return svg.encode("utf-8"), hotspots
