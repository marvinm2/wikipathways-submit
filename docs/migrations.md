# Database migrations

The registry (`wpid_reservation`, `pathway_lock`, `review`) is the transactional core (design
§4). Its schema is versioned with **Alembic**; the dev-only `create_all` shortcut runs **only**
for SQLite (`app/main.py` lifespan). On a real Postgres deployment the app never auto-creates
tables — you run migrations explicitly.

## How the URL is resolved

`migrations/env.py` reads the database URL from, in order:

1. `-x db_url=...` on the alembic command line,
2. an explicit `sqlalchemy.url` in `alembic.ini` (used by the test-suite only),
3. otherwise the app's own settings — `WPSUBMIT_DATABASE_URL` (same env var the app uses).

So migrations and the running app always target the same database without duplicated config.

## Commands

```bash
# Apply all migrations (run this on deploy, before the app starts serving)
WPSUBMIT_DATABASE_URL=postgresql+psycopg://user:pass@host/db alembic upgrade head

# Inspect / roll back
alembic current
alembic history
alembic downgrade -1

# After changing a model in app/models/, generate a new revision and review it by hand
alembic revision --autogenerate -m "describe the change"
```

## On the cluster

Run `alembic upgrade head` as a one-shot step in the deploy (container entrypoint or a
pre-start job) against the GlusterFS-backed Postgres, then start uvicorn. The migration is
idempotent — re-running `upgrade head` on an up-to-date database is a no-op. This belongs with
the rest of the cluster deployment work (issue #5).

## Test guard

`tests/test_migrations.py` upgrades a fresh database to `head` and then asserts, via Alembic
autogenerate, that there is **no** drift between the migration and the ORM models — so a model
change without a matching migration fails CI rather than silently diverging from `create_all`.
