"""review: pipeline publication state

Prepares the review table for a target repo that publishes through its own Actions
(docs/sandbox-pipeline.md) rather than through a merge by this app:

- ``wpid`` becomes nullable, because a new pathway has no id until the target repo assigns one
  at publication.
- Four new statuses (approved / published / publish_failed / rejected) join the enum's CHECK
  constraint. ``merged`` and ``closed`` stay for direct mode.
- ``head_branch`` records the PR's branch, which in pipeline mode carries a timestamp and so can
  no longer be derived from the WPID.
- ``approved_at`` / ``published_at`` / ``decided_by`` / ``decision_note`` / ``github_labels`` /
  ``last_checked_at`` carry the publication handshake and the reconcile throttle.

All of it is one revision on purpose: splitting it would leave a window where the schema drift
test fails against the models.

Revision ID: c3d9f0a1b2e4
Revises: b7c1e2f3a4d5
Create Date: 2026-07-27 18:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d9f0a1b2e4'
down_revision: str | None = 'b7c1e2f3a4d5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match app.models.ReviewStatus. The enum is native_enum=False, so on every backend it is a
# VARCHAR plus a named CHECK constraint, and widening it means rewriting that constraint.
_NEW_STATUSES = (
    'open',
    'changes_requested',
    'approved',
    'published',
    'publish_failed',
    'rejected',
    'merged',
    'closed',
)
_OLD_STATUSES = ('open', 'changes_requested', 'merged', 'closed')


def _status_type(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, native_enum=False, length=24, name='reviewstatus')


def upgrade() -> None:
    # batch_alter_table: SQLite cannot ALTER COLUMN or redefine a CHECK constraint in place, so
    # Alembic recreates the table. A no-op wrapper on PostgreSQL.
    with op.batch_alter_table('review') as batch:
        batch.add_column(sa.Column('head_branch', sa.String(length=255), nullable=True))
        batch.add_column(sa.Column('decided_by', sa.String(length=255), nullable=True))
        batch.add_column(sa.Column('decision_note', sa.Text(), nullable=True))
        batch.add_column(sa.Column('github_labels', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column('published_at', sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True))
        batch.alter_column('wpid', existing_type=sa.Integer(), nullable=True)
        batch.alter_column(
            'status',
            existing_type=_status_type(_OLD_STATUSES),
            type_=_status_type(_NEW_STATUSES),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Rows in a status this schema cannot express have to land somewhere; 'closed' is the honest
    # choice for anything that reached a decision, 'open' for anything still in flight.
    op.execute("UPDATE review SET status = 'closed' WHERE status IN ('published', 'rejected')")
    op.execute(
        "UPDATE review SET status = 'open' WHERE status IN ('approved', 'publish_failed')"
    )
    with op.batch_alter_table('review') as batch:
        batch.alter_column(
            'status',
            existing_type=_status_type(_NEW_STATUSES),
            type_=_status_type(_OLD_STATUSES),
            existing_nullable=False,
        )
        batch.alter_column('wpid', existing_type=sa.Integer(), nullable=False)
        batch.drop_column('last_checked_at')
        batch.drop_column('published_at')
        batch.drop_column('approved_at')
        batch.drop_column('github_labels')
        batch.drop_column('decision_note')
        batch.drop_column('decided_by')
        batch.drop_column('head_branch')
