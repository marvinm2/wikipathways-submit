"""The structured curation checklist (design §4.5).

A reviewer works a fixed list of checks per pathway; approval requires every *required* item to
be marked ``pass``. The template is defined once here; each Review stores per-item state as JSON
so the list can evolve without a schema migration.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class ChecklistState(enum.StrEnum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"
    NA = "na"


@dataclass(frozen=True)
class ChecklistItemDef:
    key: str
    label: str
    required: bool


# Ordered; keys are stable identifiers used by the API to update a single item.
CURATION_CHECKLIST: list[ChecklistItemDef] = [
    ChecklistItemDef("render_ok", "Rendered pathway displays correctly", required=True),
    ChecklistItemDef(
        "datanodes_mapped", "Data nodes are annotated with identifiers", required=True
    ),
    ChecklistItemDef("references_valid", "References resolve", required=True),
    ChecklistItemDef("naming_ok", "WPID and file layout are correct", required=True),
    ChecklistItemDef("description_ok", "Title and description are meaningful", required=True),
    ChecklistItemDef("ontology_tags", "Ontology tags present", required=False),
]

_VALID_KEYS = {item.key for item in CURATION_CHECKLIST}


def default_checklist() -> list[dict]:
    return [
        {
            "key": item.key,
            "label": item.label,
            "required": item.required,
            "state": ChecklistState.PENDING.value,
            "note": "",
        }
        for item in CURATION_CHECKLIST
    ]


def is_valid_key(key: str) -> bool:
    return key in _VALID_KEYS


def is_complete(checklist: list[dict]) -> bool:
    """True iff every required item is marked ``pass`` (non-required items may be anything)."""
    return all(
        item["state"] == ChecklistState.PASS.value
        for item in checklist
        if item.get("required")
    )
