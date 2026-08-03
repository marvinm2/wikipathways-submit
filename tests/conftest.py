from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.db import make_engine, make_session_factory
from app.locks import PathwayLockRegistry
from app.models import Base


@pytest.fixture
def session_factory(tmp_path) -> sessionmaker:
    # File-based SQLite (not :memory:) so multiple threads/connections share one database —
    # required to exercise the concurrent-allocation race meaningfully.
    engine = make_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def locks(session_factory) -> PathwayLockRegistry:
    """A lock registry with no open-PR scanner — for tests about what happens to a lock the app
    already holds, rather than about refusing to take one."""
    return PathwayLockRegistry(session_factory)


class RecordingPreviews:
    """Stands in for PreviewService: records which PRs were freed, and what a sweep was told.

    Shared because the two halves of issue #18 are exercised from opposite ends — the per-
    transition free from the curation tests, the sweep from the pipeline ones — and a second copy
    would be a second thing to keep in step.
    """

    def __init__(self) -> None:
        self.discarded: list[int] = []
        self.swept: list[set[int]] = []

    def discard(self, pr_number: int) -> bool:
        self.discarded.append(pr_number)
        return True

    def sweep(self, keep, *, force: bool = False) -> int:
        self.swept.append(set(keep))
        return 0
