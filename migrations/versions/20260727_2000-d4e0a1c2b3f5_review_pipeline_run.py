"""review: record the target repo's workflow outcome

A submission whose GPML the target repo cannot parse loses its metadata and its preview, and
nothing on the pull request says why — the failing job is several clicks away in Actions. This
column carries the last-seen run state so the dashboard can say it plainly.

Recorded during the throttled reconcile pass rather than fetched per page load, so the queue
costs no extra GitHub calls per row.

Revision ID: d4e0a1c2b3f5
Revises: c3d9f0a1b2e4
Create Date: 2026-07-27 20:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e0a1c2b3f5'
down_revision: str | None = 'c3d9f0a1b2e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('review', sa.Column('pipeline_run', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('review', 'pipeline_run')
