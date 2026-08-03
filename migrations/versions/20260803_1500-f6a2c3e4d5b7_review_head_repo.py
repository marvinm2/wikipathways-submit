"""review: record which repository the head branch lives on

``head_branch`` alone identifies an edit only while every branch is on the content repository,
which is true of every row written so far — the app pushes the branch there itself. It stops
being true the moment a pull request arrives from a fork, and 36 of the last 53 closed pull
requests on ``wikipathways/wikipathways-database`` did exactly that. ``submit/WP0001`` then
exists in as many repositories as there are submitters, and a revise keyed on the branch name
would find somebody else's pull request or none at all (issue #22).

NULL means the content repository, so no backfill: that is what every existing row is.

Revision ID: f6a2c3e4d5b7
Revises: e5f1b2d3c4a6
Create Date: 2026-08-03 15:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f6a2c3e4d5b7'
down_revision: str | None = 'e5f1b2d3c4a6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('review', sa.Column('head_repo', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('review', 'head_repo')
