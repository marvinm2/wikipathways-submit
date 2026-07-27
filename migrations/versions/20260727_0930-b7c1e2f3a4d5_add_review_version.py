"""add review.version optimistic-concurrency counter

Guards the checklist read-modify-write against lost updates (issue #15): the ORM stamps every
UPDATE to a review with ``WHERE version = <read value>`` and bumps it, turning a concurrent
overwrite into a StaleDataError the service retries instead of silently dropping a curator's edit.

Revision ID: b7c1e2f3a4d5
Revises: abdcdc585430
Create Date: 2026-07-27 09:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7c1e2f3a4d5'
down_revision: str | None = 'abdcdc585430'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default='0' backfills any existing rows; the model also declares it so a fresh
    # create_all and this migration agree.
    op.add_column(
        'review',
        sa.Column('version', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('review', 'version')
