"""Engine construction — specifically, surviving an idle connection drop.

The failure this guards against needs a real socket that a real network silently discards, so
what is asserted here is the configuration that prevents it rather than the reconnect itself.
That is deliberate: the bug shipped because nothing recorded the intent.

The Postgres driver lives in the ``postgres`` extra, not ``dev``, so these tests must not build
a real Postgres engine — they capture what ``make_engine`` hands to SQLAlchemy instead.
"""
from __future__ import annotations

import app.db as db_module
from app.db import make_engine

PG_URL = "postgresql+psycopg://u:p@example.invalid:5432/db"


def _captured_kwargs(monkeypatch, url, **overrides):
    seen = {}
    real_create_engine = db_module.create_engine

    def fake_create_engine(passed_url, **kwargs):
        seen.update(kwargs)
        seen["url"] = passed_url
        # Hand back a real (throwaway, in-memory) engine so the SQLite branch can still attach
        # its pragma listener — the point here is the kwargs, not the object.
        return real_create_engine("sqlite://")

    monkeypatch.setattr(db_module, "create_engine", fake_create_engine)
    make_engine(url, **overrides)
    return seen


def test_postgres_engine_checks_liveness_before_handing_out_a_connection(monkeypatch):
    # A pooled connection outlives its request, and the Swarm overlay network drops idle TCP
    # sessions without telling either end. Without pre-ping the pool serves the dead socket, so
    # the first request after any quiet period 500s with "server closed the connection
    # unexpectedly" — observed live on upload.wikipathways.org after roughly 24 minutes idle,
    # which is exactly when a curator comes back to the dashboard.
    kwargs = _captured_kwargs(monkeypatch, PG_URL)
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] == 1800


def test_engine_pool_settings_stay_overridable(monkeypatch):
    kwargs = _captured_kwargs(monkeypatch, PG_URL, pool_pre_ping=False, pool_recycle=60)
    assert kwargs["pool_pre_ping"] is False
    assert kwargs["pool_recycle"] == 60


def test_sqlite_is_left_alone(monkeypatch):
    # SQLite is a local file with no socket to drop; pre-ping and recycle would be noise, and
    # the test harness leans on its own pooling.
    kwargs = _captured_kwargs(monkeypatch, "sqlite://")
    assert "pool_pre_ping" not in kwargs
    assert "pool_recycle" not in kwargs
    assert kwargs["connect_args"] == {"check_same_thread": False}


def test_sqlite_engine_still_builds_and_applies_its_pragmas(tmp_path):
    # The real thing, unmonkeypatched: the pragma listener still registers.
    engine = make_engine(f"sqlite:///{tmp_path / 'x.db'}")
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert mode.lower() == "wal"
