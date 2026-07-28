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
