"""Pathway preview service (issue #11) — the before/after render the dashboard shows.

The app draws both sides itself (``app/preview/render.py``) when the PR is created and caches the
SVGs on disk, so a curator sees the diagram immediately with no CI wait.

There is no second source. CI used to render with PinPath and upload the SVGs as a run artifact,
and this service downloaded them; that path was retired on 2026-07-27 along with the render step
itself (``mvp1/pr-preview.yml`` now converts to pvjson only — a PR comment cannot show an image
anyway). What CI produces is validation, not pictures, so nothing here talks to GitHub.

Cached per PR under ``<cache_dir>/<pr_number>/``: ``before.svg`` / ``after.svg``, the
``metadata.json`` sidecar the dashboard panel reads, and a ``render-failed`` marker when a GPML
could not be drawn (so the queue can say so instead of spinning on "generating" forever).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.preview.metadata import parse_curation_metadata
from app.preview.render import RenderError, render_gpml_with_nodes

_SIDES = ("before", "after")
_FAILED_MARKER = "render-failed"


@dataclass(frozen=True)
class PreviewState:
    status: str  # 'pending' | 'ready' | 'failed'
    has_before: bool = False
    has_after: bool = False


class PreviewService:
    def __init__(self, *, cache_dir: str | Path) -> None:
        self._cache_dir = Path(cache_dir)

    # -- render at PR-creation time (issue #11, 1a) ----------------------------------------
    def render_local(
        self,
        pr_number: int,
        wpid: int,
        *,
        after_gpml: bytes,
        before_gpml: bytes | None = None,
        submitter_note: str | None = None,
    ) -> PreviewState:
        """Render the before/after SVGs in-process and cache them on disk.

        Best-effort: a side that can't be rendered is skipped, never raised — the preview must
        never sink the submission. The curation metadata parsed from the *after* GPML (data nodes,
        references, description, ontology tags) plus the submitter's note is cached alongside for
        the dashboard panel.
        """
        out = self._cache_dir / str(pr_number)
        out.mkdir(parents=True, exist_ok=True)
        rendered: dict[str, bool] = {"before": False, "after": False}
        for side, gpml in (("before", before_gpml), ("after", after_gpml)):
            if gpml is None:
                continue
            try:
                svg, hotspots = render_gpml_with_nodes(gpml)
            except RenderError:
                continue
            (out / f"{side}.svg").write_bytes(svg)
            # The clickable-node overlay (issue #14). Written beside the drawing it belongs to,
            # so a side that failed to render has no stale hotspots left pointing at nothing.
            try:
                (out / f"{side}-nodes.json").write_text(
                    json.dumps([h.as_dict() for h in hotspots]), encoding="utf-8"
                )
            except OSError:
                pass  # the overlay is an enhancement; never fail a render on it
            rendered[side] = True
        self._write_metadata(out, after_gpml, submitter_note)
        drawn = rendered["before"] or rendered["after"]
        # Record the outcome so a re-upload that now renders clears a previous failure, and a
        # GPML we cannot draw reads as 'failed' rather than 'pending' forever.
        marker = out / _FAILED_MARKER
        if drawn:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text("", encoding="utf-8")
        return PreviewState(
            "ready" if drawn else "failed",
            has_before=rendered["before"],
            has_after=rendered["after"],
        )

    def _local_side_exists(self, pr_number: int) -> bool:
        d = self._cache_dir / str(pr_number)
        return any((d / f"{s}.svg").is_file() for s in _SIDES)

    # -- curation metadata sidecar (dashboard panel) --------------------------------------
    @staticmethod
    def _write_metadata(out: Path, after_gpml: bytes, submitter_note: str | None) -> None:
        """Cache the parsed curation metadata + submitter note next to the SVGs (best-effort)."""
        try:
            data = parse_curation_metadata(after_gpml).as_dict()
            data["submitter_note"] = (submitter_note or "").strip()
            (out / "metadata.json").write_text(json.dumps(data), encoding="utf-8")
        except (OSError, ValueError):
            pass  # the panel is cosmetic; never fail the write path on it

    def metadata(self, pr_number: int) -> dict | None:
        """The cached curation metadata for a PR (data nodes, references, description, ontology
        tags, submitter note), or None if it was never rendered."""
        path = self._cache_dir / str(pr_number) / "metadata.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- queue render ----------------------------------------------------------------------
    def status(self, pr_number: int) -> str:
        """'ready' once a side is on disk, 'failed' if the GPML could not be drawn, else
        'pending' (nothing rendered yet, or the cache was cleared under a live review)."""
        if self._local_side_exists(pr_number):
            return "ready"
        if (self._cache_dir / str(pr_number) / _FAILED_MARKER).is_file():
            return "failed"
        return "pending"

    def nodes(self, pr_number: int, side: str) -> list[dict] | None:
        """Clickable data-node hotspots for one side (issue #14), or None when there are none on
        file — a side that was never rendered, or a cache written before this existed."""
        if side not in _SIDES:
            return None
        path = self._cache_dir / str(pr_number) / f"{side}-nodes.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, list) else None

    def svg_path(self, pr_number: int, side: str) -> Path | None:
        """Cached path to the before/after SVG, or None when that side wasn't rendered (a new
        pathway has no "before") — the endpoint then serves a placeholder."""
        if side not in _SIDES:
            return None
        path = self._cache_dir / str(pr_number) / f"{side}.svg"
        return path if path.is_file() else None
