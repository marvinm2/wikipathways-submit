"""review: keep the submitter's note on the review row

The note was written only into the pull request body. A target repo that publishes through its
own Actions rewrites that body with its template, which discards it silently — the app's write
succeeds and the loss happens afterwards in someone else's workflow (issue #25). The dashboard
was unaffected because it reads a copy cached beside the render, but that copy is deleted at
every terminal transition by design and never exists at all for a GPML the renderer refuses.

Storing it here makes it available wherever the review is, which is what lets the mirror comment
carry it back to GitHub.

Revision ID: e5f1b2d3c4a6
Revises: d4e0a1c2b3f5
Create Date: 2026-07-29 11:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5f1b2d3c4a6'
down_revision: str | None = 'd4e0a1c2b3f5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('review', sa.Column('submitter_note', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('review', 'submitter_note')
