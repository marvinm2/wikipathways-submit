"""The staged copies in ``sandbox-workflows/`` have to be valid before anyone dispatches them.

These files are not run by this app — they are repaired copies of the target repository's own
workflows, staged for a pull request there and already installed on the fork. That is exactly why
they need a test: nothing here imports them, so a file that GitHub refuses to parse looks
completely fine from inside this repo.

It happened. The repaired 3a carried a ``# FIX:`` comment inside a ``run:`` block that quoted the
commit message it replaced, and the quotation contained a GitHub expression. A ``run:`` block is
one string value and the runner substitutes expressions into its *text* before bash ever sees it,
so the ``#`` protects nothing — it is a bash comment, and substitution happens before bash exists.
The expression did not parse, which does not fail a step but fails the **whole workflow at
startup**:

    HTTP 422: failed to parse workflow: (Line: 224, Col: 14): Unexpected symbol: '...wpid'

The line it names is the ``run:``, not the comment below it. The workflow had sat that way since
it was written, because nothing had ever dispatched 3a.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "sandbox-workflows" / ".github" / "workflows"

#: What a GitHub expression is allowed to look like here. Deliberately strict: every expression in
#: these files is a plain reference (``steps.x.outputs.y``, ``secrets.NAME``, ``github.event…``),
#: so anything else is far more likely to be prose that wandered into a string than a construct
#: worth supporting. Widen it when a real expression needs it, not to make a failure go away.
_PLAIN_REFERENCE = re.compile(r"[A-Za-z_][\w.\-\[\]'\" ]*")

_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml"))


def _walk_run_blocks(node, path: str = ""):
    """Yield ``(path, script)`` for every ``run:`` value in the document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                yield path, value
            yield from _walk_run_blocks(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_run_blocks(value, f"{path}[{index}]")


def test_there_are_workflows_to_check():
    """Guard against the glob silently matching nothing and every test below vacuously passing."""
    assert _workflow_files(), f"no workflow files found under {WORKFLOW_DIR}"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_is_valid_yaml(path: Path):
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} did not parse to a mapping"
    # `on:` is the YAML 1.1 boolean `True` once parsed, which is a fine reminder that this only
    # checks the file is loadable — GitHub's own schema is not modelled here.
    assert "jobs" in document, f"{path.name} declares no jobs"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_run_blocks_contain_only_parseable_expressions(path: Path):
    """No expression anywhere in a ``run:`` block, comment or not, that GitHub would reject.

    A commented-out expression is the case worth catching, because it is the one a reader assumes
    is inert.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    offenders = [
        (where, expression.strip())
        for where, script in _walk_run_blocks(document)
        for expression in _EXPRESSION.findall(script)
        if not _PLAIN_REFERENCE.fullmatch(expression.strip())
    ]
    assert not offenders, (
        f"{path.name} has expressions inside run: blocks that GitHub will refuse to parse, "
        f"failing the whole workflow at startup: {offenders}. A '#' does not make one safe — "
        f"the runner substitutes into the script text before bash sees it."
    )
