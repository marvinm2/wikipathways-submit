"""Engine / session construction.

The registry must be transactional (atomic WPID reservation, lock acquisition). PostgreSQL is
the production target; SQLite is fine for dev and tests. For SQLite we enable WAL + a busy
timeout so concurrent writers serialize with a wait rather than failing instantly — the
allocator's insert-retry loop still handles the rare genuine conflict.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str, **kwargs) -> Engine:
    """Create an Engine, applying SQLite concurrency pragmas when relevant."""
    connect_args = {}
    if url.startswith("sqlite"):
        # Allow the connection to be shared across threads in the test race harness;
        # each session still checks out its own connection from the pool.
        connect_args["check_same_thread"] = False
    engine = create_engine(url, future=True, connect_args=connect_args, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error, always close."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
