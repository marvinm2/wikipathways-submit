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
from xml.sax.saxutils import escape

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
    return svg.encode("utf-8")
