"""Alembic environment — migrations for the registry (WpidReservation / PathwayLock / Review).

The DB URL is resolved from the app's own settings so migrations and the running app never
disagree about which database they target. Override per-invocation with ``-x db_url=...``.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import Settings
from app.models import Base

config = context.config
if config.config_file_name is not None:
    # `disable_existing_loggers=False` is not alembic's default, and the default is wrong for any
    # caller that runs alembic in-process: `fileConfig` would silently disable every logger not
    # named in alembic.ini, which is all of `wpsubmit.*`. Production never noticed, because the
    # entrypoint runs `alembic upgrade head` as its own process before uvicorn starts — but the
    # test suite runs it in-process, and it switched off the app's logging for every test that
    # happened to run afterwards.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    # Precedence: `-x db_url=...` > an explicit alembic.ini sqlalchemy.url (used by tests) >
    # the app's own env-driven settings.
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    return override or config.get_main_option("sqlalchemy.url") or Settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,  # SQLite needs batch mode for ALTER
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
