"""GPML inspection + app-owned naming/layout (design §4.1).

Malformed submissions were a top-five pain (audit #94: ``ketogenic in epilepstogenesis.gpml``,
no WPID, wrong path). The app makes them impossible by owning naming and layout: it validates
the upload, assigns the WPID *into* the GPML, and lays the files out deterministically.

Where the WPID lives: GPML2013a carries it in the root ``<Pathway>`` ``Version`` attribute as
``WP<id>_r<revision>`` (e.g. ``Version="WP5636_r20260520113005"``). Assigning a WPID therefore
means rewriting that attribute, not just renaming the file.

Edits are done with targeted regex on the opening ``<Pathway>`` tag rather than a full XML
round-trip: GPML has a default namespace and ElementTree would rewrite prefixes/formatting
across the whole document. A surgical attribute edit keeps the diff minimal and faithful.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

# Opening <Pathway ...> tag (with its attributes), first occurrence, possibly multi-line.
_PATHWAY_TAG_RE = re.compile(r"<Pathway\b[^>]*>", re.DOTALL)
_WPID_IN_VERSION_RE = re.compile(r"^(WP\d+)")


class InvalidGpml(ValueError):
    """The uploaded content is not a usable GPML pathway."""

    def __init__(self, reasons: list[str] | str) -> None:
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class PathwayMeta:
    name: str | None
    organism: str | None
    version: str | None
    wpid: str | None  # parsed from Version, if present


def _as_text(content: bytes | str) -> str:
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content


def _pathway_tag(text: str) -> str:
    m = _PATHWAY_TAG_RE.search(text)
    if not m:
        raise InvalidGpml("no <Pathway> root element found")
    return m.group(0)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}="([^"]*)"', tag)
    return m.group(1) if m else None


def parse_pathway_meta(content: bytes | str) -> PathwayMeta:
    """Extract Name / Organism / Version (and the embedded WPID) from a GPML upload."""
    tag = _pathway_tag(_as_text(content))
    version = _attr(tag, "Version")
    wpid = None
    if version:
        m = _WPID_IN_VERSION_RE.match(version)
        wpid = m.group(1) if m else None
    return PathwayMeta(
        name=_attr(tag, "Name"),
        organism=_attr(tag, "Organism"),
        version=version,
        wpid=wpid,
    )


def validate_gpml(content: bytes | str) -> PathwayMeta:
    """Validate an upload is a plausible GPML pathway; return its metadata or raise InvalidGpml.

    Checks the minimum the downstream pipeline needs: a ``<Pathway>`` root, a Name, and an
    Organism (meta-data-action and the render both key off Organism).
    """
    text = _as_text(content)
    reasons: list[str] = []
    try:
        meta = parse_pathway_meta(text)
    except InvalidGpml as exc:
        raise InvalidGpml(exc.reasons) from exc
    if "http://pathvisio.org/GPML" not in text:
        reasons.append("missing GPML namespace (not a PathVisio/WikiPathways GPML file)")
    if not meta.name:
        reasons.append("root <Pathway> has no Name")
    if not meta.organism:
        reasons.append("root <Pathway> has no Organism (required for metadata + render)")
    if reasons:
        raise InvalidGpml(reasons)
    return meta


def _now_revision() -> str:
    return datetime.now(UTC).strftime("r%Y%m%d%H%M%S")


def assign_wpid(
    content: bytes | str, wpid: int, *, revision: str | None = None, author: str | None = None
) -> str:
    """Return the GPML with its root ``Version`` set to ``WP<wpid>_r<revision>``.

    Adds the Version attribute if the upload lacks one. Idempotent in shape: re-assigning
    overwrites any previous WPID, so a mis-numbered submission (audit #94) is normalized.

    ``author`` fills the GPML2013a ``Author="[name]"`` attribute **only when the upload has
    none**: existing authorship is never rewritten, but a hand-made GPML that omits it gets the
    submitter. meta-data-action dereferences that attribute unconditionally, so a file without
    it crashes the generator and the PR preview loses its metadata tables.
    """
    return assign_wpid_str(content, f"WP{wpid}", revision=revision, author=author)


def assign_wpid_str(
    content: bytes | str,
    wpid_str: str,
    *,
    revision: str | None = None,
    author: str | None = None,
) -> str:
    """``assign_wpid`` for an identifier that is not a plain integer.

    Exists for the placeholder a pipeline-mode submission carries before the target repo assigns
    a real id: the file is committed as ``WP0001.gpml``, so the ``Version`` attribute has to say
    ``WP0001`` too. Going through ``assign_wpid(1)`` would write ``WP1`` — a different, real
    pathway — and leave the file and its own metadata disagreeing.
    """
    text = _as_text(content)
    m = _PATHWAY_TAG_RE.search(text)
    if not m:
        raise InvalidGpml("no <Pathway> root element found")
    rev = revision or _now_revision()
    version_value = f"{wpid_str}_{rev}"
    tag = m.group(0)
    if re.search(r'\bVersion="[^"]*"', tag):
        new_tag = re.sub(r'\bVersion="[^"]*"', f'Version="{version_value}"', tag, count=1)
    else:
        new_tag = tag.replace("<Pathway", f'<Pathway Version="{version_value}"', 1)
    if author and not _attr(new_tag, "Author"):
        new_tag = new_tag.replace("<Pathway", f'<Pathway Author="[{author}]"', 1)
    return text[: m.start()] + new_tag + text[m.end() :]


def layout_paths(wpid: int) -> dict[str, str]:
    """Deterministic repo layout for a WPID: the canonical GPML path.

    Only the GPML is written by a submission; every other file under ``pathways/WP<id>/`` is a
    derived artifact regenerated by the pipeline, never authored by the app.
    """
    base = f"pathways/WP{wpid}"
    return {"gpml": f"{base}/WP{wpid}.gpml"}


#: The id a new pathway carries until the target repo assigns a real one (pipeline mode).
#: ``WP1`` was never available — ``pathways/WP1/`` is a real pathway — and the target repo's own
#: test submission used ``WP0001``, so this matches what its maintainers already expect.
PLACEHOLDER_WPID_STR = "WP0001"
PLACEHOLDER_GPML_PATH = f"pathways/{PLACEHOLDER_WPID_STR}/{PLACEHOLDER_WPID_STR}.gpml"
