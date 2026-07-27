"""The structured curation checklist (design §4.5) — modular, with automatable checks.

A reviewer works a fixed list of checks per pathway; approval requires every *required* item to be
marked ``pass``. Each item's state is stored per-review as JSON, so the template can change without
a schema migration.

Two kinds of automation, both **advisory** — a curator can always overrule an auto-derived state:

- ``auto_check`` — given the pathway's parsed metadata (``app.preview.metadata.CurationMetadata``),
  return a suggested state + explanatory note. E.g. "all data nodes have identifiers" → ``pass``.
  Items with an ``auto_check`` are pre-filled when the review is created and flagged ``auto`` so the
  UI shows they were machine-suggested.
- ``relevant_for_update`` — given (before, after) metadata, whether the check applies to *this*
  update. An update that didn't touch the title shouldn't ask the curator to re-check the title.
  Irrelevant items are auto-set to ``na`` and made non-blocking, with a note saying why.

## Adding or changing a check

1. Append a ``ChecklistItemDef(key, label, required=...)`` to ``CURATION_CHECKLIST``. ``key`` is a
   stable identifier the API uses to update the item — never reuse or rename one in place.
2. To automate it, pass ``auto_check=<fn(metadata) -> AutoResult>``. Keep it cheap and offline
   (no network) — it runs on every submission; verification that needs the network (does an id
   actually resolve?) belongs in a note with a link, not the auto_check.
3. To scope it to relevant updates, pass ``relevant_for_update=<fn(before, after) -> bool>``.
4. No migration is needed — the checklist is a JSON column. Existing open reviews keep the checklist
   they were created with; only new reviews pick up the change.
"""
from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass


class ChecklistState(enum.StrEnum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"
    NA = "na"


@dataclass(frozen=True)
class AutoResult:
    state: str
    note: str = ""


# Metadata is duck-typed (``app.preview.metadata.CurationMetadata``) to avoid an import cycle with
# the models, which import ``default_checklist`` from here.
MetaFn = Callable[[object], AutoResult]
RelevanceFn = Callable[[object, object], bool]


@dataclass(frozen=True)
class ChecklistItemDef:
    key: str
    label: str
    required: bool
    auto_check: MetaFn | None = None
    relevant_for_update: RelevanceFn | None = None


# ---- auto-check implementations (offline, cheap) -----------------------------------------


def _auto_datanodes(m) -> AutoResult:
    nodes = m.data_nodes
    if not nodes:
        return AutoResult(ChecklistState.NA.value, "No data nodes in this pathway.")
    unmapped = [n.label or "(unlabeled)" for n in nodes if not n.identifier]
    if unmapped:
        shown = ", ".join(unmapped[:6]) + ("…" if len(unmapped) > 6 else "")
        return AutoResult(
            ChecklistState.FAIL.value,
            f"{len(unmapped)} of {len(nodes)} data nodes have no identifier: {shown}",
        )
    return AutoResult(
        ChecklistState.PASS.value, f"All {len(nodes)} data nodes carry an identifier."
    )


def _auto_references(m) -> AutoResult:
    if not m.references:
        return AutoResult(ChecklistState.NA.value, "No literature references to resolve.")
    # Resolvability needs the network; the References panel lists each one as a clickable link, so
    # point the curator there rather than guessing pass/fail.
    return AutoResult(
        ChecklistState.PENDING.value,
        f"Open each of the {len(m.references)} reference(s) above to confirm it resolves.",
    )


def _auto_naming(m) -> AutoResult:
    return AutoResult(ChecklistState.PASS.value, "WPID and file layout are assigned by the app.")


def _auto_description(m) -> AutoResult:
    missing = []
    if not m.name:
        missing.append("title")
    if not (m.description or "").strip():
        missing.append("description")
    if missing:
        return AutoResult(
            ChecklistState.PENDING.value,
            f"No {', '.join(missing)} — confirm this is intentional.",
        )
    return AutoResult(ChecklistState.PASS.value, "Title and description are present.")


def _auto_ontology(m) -> AutoResult:
    if m.ontology_tags:
        return AutoResult(ChecklistState.PASS.value, f"{len(m.ontology_tags)} ontology tag(s).")
    return AutoResult(ChecklistState.NA.value, "No ontology tags (optional).")


# ---- update-relevance implementations ----------------------------------------------------


def _nodes_key(m) -> list:
    return sorted((n.label, n.database, n.identifier) for n in m.data_nodes)


def _refs_key(m) -> list:
    return sorted((r.database, r.identifier) for r in m.references)


def _onto_key(m) -> list:
    return sorted((t.ontology, t.identifier) for t in m.ontology_tags)


def _rel_datanodes(before, after) -> bool:
    return _nodes_key(before) != _nodes_key(after)


def _rel_description(before, after) -> bool:
    return (before.name, before.description) != (after.name, after.description)


def _rel_references(before, after) -> bool:
    return _refs_key(before) != _refs_key(after)


def _rel_ontology(before, after) -> bool:
    return _onto_key(before) != _onto_key(after)


# Ordered; keys are stable identifiers used by the API to update a single item.
CURATION_CHECKLIST: list[ChecklistItemDef] = [
    # A human still eyeballs the rendered diagram — no auto_check.
    ChecklistItemDef("render_ok", "Rendered pathway displays correctly", required=True),
    ChecklistItemDef(
        "datanodes_mapped",
        "Data nodes are annotated with identifiers",
        required=True,
        auto_check=_auto_datanodes,
        relevant_for_update=_rel_datanodes,
    ),
    ChecklistItemDef(
        "references_valid",
        "References resolve",
        required=True,
        auto_check=_auto_references,
        relevant_for_update=_rel_references,
    ),
    ChecklistItemDef(
        "naming_ok", "WPID and file layout are correct", required=True, auto_check=_auto_naming
    ),
    ChecklistItemDef(
        "description_ok",
        "Title and description are meaningful",
        required=True,
        auto_check=_auto_description,
        relevant_for_update=_rel_description,
    ),
    ChecklistItemDef(
        "ontology_tags",
        "Ontology tags present",
        required=False,
        auto_check=_auto_ontology,
        relevant_for_update=_rel_ontology,
    ),
]

_VALID_KEYS = {item.key for item in CURATION_CHECKLIST}


def build_checklist(*, metadata=None, before=None, kind: str = "new") -> list[dict]:
    """Build a review's checklist, applying auto-checks and (for updates) relevance scoping.

    ``metadata`` is the parsed *after* metadata; ``before`` the base-version metadata (updates
    only). With neither, every item is a blank ``pending`` — the plain template.
    """
    items: list[dict] = []
    for d in CURATION_CHECKLIST:
        # Scope updates: an item whose subject didn't change is auto-N/A and non-blocking.
        if (
            kind == "update"
            and before is not None
            and metadata is not None
            and d.relevant_for_update is not None
            and not d.relevant_for_update(before, metadata)
        ):
            items.append(
                {
                    "key": d.key,
                    "label": d.label,
                    "required": False,
                    "state": ChecklistState.NA.value,
                    "note": "Not relevant — unchanged in this update.",
                    "auto": True,
                }
            )
            continue

        required = d.required
        state, note, auto = ChecklistState.PENDING.value, "", False
        if metadata is not None and d.auto_check is not None:
            res = d.auto_check(metadata)
            state, note, auto = res.state, res.note, True
            # An auto "N/A" means there is nothing to check (e.g. no references), so it must not
            # block approval — drop the requirement rather than force a manual override.
            if state == ChecklistState.NA.value:
                required = False
        items.append(
            {
                "key": d.key,
                "label": d.label,
                "required": required,
                "state": state,
                "note": note,
                "auto": auto,
            }
        )
    return items


def default_checklist() -> list[dict]:
    """The plain, all-pending checklist (SQLAlchemy column default; no metadata available)."""
    return build_checklist(kind="new", metadata=None)


def is_valid_key(key: str) -> bool:
    return key in _VALID_KEYS


def is_complete(checklist: list[dict]) -> bool:
    """True iff every required item is marked ``pass`` (non-required items may be anything)."""
    return all(
        item["state"] == ChecklistState.PASS.value
        for item in checklist
        if item.get("required")
    )
