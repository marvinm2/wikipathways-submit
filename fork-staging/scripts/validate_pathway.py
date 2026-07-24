#!/usr/bin/env python3
"""Validate a single WikiPathways pathway for the PR-preview pipeline (MVP-1).

Ships to wikipathways-database as scripts/validate_pathway.py and is invoked by
.github/workflows/pr-preview.yml once the review artifacts have been generated
for a changed pathway. It does NOT modify the pathway; it only reports.

Design reference: docs/design-proposal.md §4.4 (the validation report is the one
piece of the review subset that does not already exist in on_gpml_change.yml).

Usage:
    python scripts/validate_pathway.py WP1234 [--pathways-dir pathways] \
        [--rendered path/to/WP1234.svg] [--out report.md]

Exit code is always 0 (a validation report is informational, not a gate — a
curator decides). The overall severity is written to $GITHUB_OUTPUT as
`status=pass|warn|fail` when that env var is set, so the workflow can label the
run without parsing markdown.

Stdlib only — no pip install, so the job stays cheap and cannot break on deps.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

# Severity ordering so we can take the worst finding as the overall status.
PASS, WARN, FAIL = "pass", "warn", "fail"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}
_ICON = {PASS: "✅", WARN: "⚠️", FAIL: "❌"}

# An empty BridgeDb/Xref identifier in a generated datanodes.tsv row shows up as
# an empty Identifier column; meta-data-action writes literal "NA" when it cannot
# resolve a datasource, so both mean "unmapped".
_UNMAPPED = {"", "na", "null", "none"}


class Report:
    def __init__(self, wpid: str) -> None:
        self.wpid = wpid
        self.checks: list[tuple[str, str, str]] = []  # (severity, name, detail)

    def add(self, severity: str, name: str, detail: str) -> None:
        self.checks.append((severity, name, detail))

    @property
    def status(self) -> str:
        worst = PASS
        for severity, _, _ in self.checks:
            if _RANK[severity] > _RANK[worst]:
                worst = severity
        return worst

    def to_markdown(self) -> str:
        lines = [
            f"### 🔬 Pathway validation — `{self.wpid}`",
            "",
            f"**Overall: {_ICON[self.status]} {self.status.upper()}**",
            "",
            "| | Check | Detail |",
            "|---|---|---|",
        ]
        for severity, name, detail in self.checks:
            detail = detail.replace("\n", " ").replace("|", "\\|")
            lines.append(f"| {_ICON[severity]} | {name} | {detail} |")
        lines.append("")
        lines.append(
            "_This is an automated preview, not a merge gate. A curator makes the "
            "final call._"
        )
        return "\n".join(lines)


def _read_gpml_text(gpml_path: Path) -> str | None:
    try:
        return gpml_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def check_gpml(report: Report, gpml_path: Path) -> None:
    """Lightweight, schema-version-agnostic checks straight off the GPML text.

    We deliberately avoid a full XML parse: GPML exists in 2013a and 2021
    flavours with different namespaces, and the generated TSV/JSON (checked
    below) is the normalized view we trust for identifier coverage. Here we only
    catch the couple of raw-XML problems the pipeline is known to trip on.
    """
    text = _read_gpml_text(gpml_path)
    if text is None:
        report.add(FAIL, "GPML present", f"could not read {gpml_path}")
        return
    report.add(PASS, "GPML present", f"{gpml_path.name} ({len(text):,} bytes)")

    # Empty <bp:ID></bp:ID> breaks meta-data-action (Citation must have valid
    # xref or url). on-gpml-change_local.sh rewrites these to NA before running;
    # surface how many so the submitter can fix the source instead.
    # Match an empty bp:ID whether or not it carries attributes, mirroring the
    # `s/></bp:ID>/` cleanup in scripts/local-run/on-gpml-change_local.sh.
    empty_bp_id = len(re.findall(r"<bp:ID\b[^>]*/>|<bp:ID\b[^>]*>\s*</bp:ID>", text))
    if empty_bp_id:
        report.add(
            WARN,
            "Empty citation IDs",
            f"{empty_bp_id} empty <bp:ID> element(s); auto-rewritten to NA for "
            "rendering — fix at source.",
        )
    else:
        report.add(PASS, "Empty citation IDs", "none")

    # DataNode count is a cheap sanity signal — a pathway with zero is malformed.
    datanode_count = len(re.findall(r"<DataNode\b", text))
    if datanode_count == 0:
        report.add(FAIL, "DataNodes in GPML", "0 — pathway has no data nodes")
    else:
        report.add(PASS, "DataNodes in GPML", str(datanode_count))


def check_info_json(report: Report, info_path: Path) -> None:
    if not info_path.exists():
        report.add(
            FAIL,
            "Metadata generated",
            f"{info_path.name} missing — meta-data-action did not produce info.json",
        )
        return
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.add(FAIL, "Metadata generated", f"{info_path.name} unreadable: {exc}")
        return
    if not isinstance(info, dict):
        # Valid JSON that isn't an object (null / list / scalar) — e.g. a stub
        # written on generator failure. Guard the .get() calls below.
        report.add(FAIL, "Metadata generated", f"{info_path.name} is not a JSON object")
        return
    report.add(PASS, "Metadata generated", info_path.name)

    title = (info.get("title") or "").strip()
    report.add(PASS if title else FAIL, "Title", title or "missing")

    description = (info.get("description") or "").strip()
    if not description:
        report.add(WARN, "Description", "empty")
    else:
        report.add(PASS, "Description", f"{len(description)} chars")

    organisms = info.get("organisms") or []
    report.add(
        PASS if organisms else WARN,
        "Organism",
        ", ".join(organisms) if organisms else "none set",
    )

    authors = info.get("authors") or []
    report.add(
        PASS if authors else WARN,
        "Authors",
        ", ".join(authors) if authors else "none listed",
    )

    ontology_ids = info.get("ontology-ids") or []
    # Most curation friction is untagged pathways; a warning is a nudge, not a block.
    report.add(
        PASS if ontology_ids else WARN,
        "Ontology tags",
        f"{len(ontology_ids)} tag(s)"
        if ontology_ids
        else "none — add Pathway/Disease/Cell-type ontology terms",
    )


def check_datanodes(report: Report, datanodes_path: Path) -> None:
    if not datanodes_path.exists():
        report.add(
            FAIL,
            "Datanode table",
            f"{datanodes_path.name} missing — meta-data-action did not run",
        )
        return
    try:
        with datanodes_path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))
    except (OSError, csv.Error) as exc:
        # csv.Error (not an OSError) fires on an oversized field — an untrusted
        # DataNode label/comment can exceed the default field-size limit.
        report.add(FAIL, "Datanode table", f"{datanodes_path.name} unreadable: {exc}")
        return

    total = len(rows)
    if total == 0:
        report.add(WARN, "Datanode annotation", "table is empty")
        return

    unmapped = [
        (r.get("Label") or "?").strip()
        for r in rows
        if (r.get("Identifier") or "").strip().lower() in _UNMAPPED
    ]
    mapped = total - len(unmapped)
    if unmapped:
        preview = ", ".join(sorted(set(unmapped))[:8])
        more = f" (+{len(set(unmapped)) - 8} more)" if len(set(unmapped)) > 8 else ""
        severity = FAIL if mapped == 0 else WARN
        report.add(
            severity,
            "Datanode annotation",
            f"{mapped}/{total} mapped; {len(unmapped)} without identifier: "
            f"{preview}{more}",
        )
    else:
        report.add(PASS, "Datanode annotation", f"{mapped}/{total} mapped")


def check_refs(report: Report, refs_path: Path, bibliography_path: Path) -> None:
    # refs.tsv (raw ref list) is produced by meta-data-action; bibliography.tsv
    # (formatted) by generate-references. Report on whichever exists.
    target = refs_path if refs_path.exists() else bibliography_path
    if not target.exists():
        report.add(WARN, "References", "no reference table generated")
        return
    try:
        with target.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh, delimiter="\t"))
    except (OSError, csv.Error) as exc:
        report.add(WARN, "References", f"{target.name} unreadable: {exc}")
        return
    n = max(0, len(rows) - 1)  # minus header
    report.add(
        PASS if n else WARN,
        "References",
        f"{n} reference(s) in {target.name}" if n else "none cited",
    )


def check_render(report: Report, rendered: Path | None) -> None:
    if rendered is None:
        report.add(WARN, "Rendered SVG", "not provided to validator")
        return
    if rendered.exists() and rendered.stat().st_size > 0:
        report.add(PASS, "Rendered SVG", f"{rendered.name} ({rendered.stat().st_size:,} bytes)")
    else:
        report.add(FAIL, "Rendered SVG", "render step produced no SVG")


def build_report(wpid: str, pathways_dir: Path, rendered: Path | None) -> Report:
    d = pathways_dir / wpid
    report = Report(wpid)
    check_gpml(report, d / f"{wpid}.gpml")
    check_info_json(report, d / f"{wpid}-info.json")
    check_datanodes(report, d / f"{wpid}-datanodes.tsv")
    check_refs(report, d / f"{wpid}-refs.tsv", d / f"{wpid}-bibliography.tsv")
    check_render(report, rendered)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate one pathway for PR preview.")
    ap.add_argument("wpid", help="e.g. WP1234")
    ap.add_argument("--pathways-dir", default="pathways", type=Path)
    ap.add_argument("--rendered", type=Path, default=None, help="path to the rendered SVG")
    ap.add_argument("--out", type=Path, default=None, help="write markdown here (else stdout)")
    ap.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="write just pass|warn|fail here (for the workflow to roll up)",
    )
    args = ap.parse_args(argv)

    # Tolerate oversized fields (a huge DataNode label/comment) instead of
    # crashing csv — keeps the "always exit 0" contract for untrusted input.
    try:
        csv.field_size_limit(16 * 1024 * 1024)
    except (OverflowError, ValueError):
        pass

    report = build_report(args.wpid, args.pathways_dir, args.rendered)
    md = report.to_markdown()

    if args.out:
        args.out.write_text(md + "\n", encoding="utf-8")
    else:
        print(md)

    if args.status_file:
        args.status_file.write_text(report.status + "\n", encoding="utf-8")

    # Expose the overall status to the workflow without markdown parsing.
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"status={report.status}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
