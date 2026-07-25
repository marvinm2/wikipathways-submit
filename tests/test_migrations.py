"""The Alembic migration must build the exact schema the models declare (issue #2).

Guards the production path (``alembic upgrade head``) against drift from the dev-only
``create_all``: upgrade a fresh DB, then assert autogenerate finds *no* difference versus the
models. If someone adds a column to a model without a migration, this fails.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.models import Base

_REPO = Path(__file__).resolve().parent.parent


def _config(url: str) -> Config:
    cfg = Config(str(_REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_upgrade_head_creates_all_tables(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_config(url), "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"pathway_lock", "review", "wpid_reservation"} <= tables


def test_migration_matches_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_config(url), "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diffs = compare_metadata(ctx, Base.metadata)
    assert diffs == [], f"schema drift between migration head and models: {diffs}"
