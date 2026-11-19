"""store whether the current search page has more NCBI hits

Revision ID: a8b9c0d1e2f3
Revises: f7b8c9d0e1f2
Create Date: 2026-11-19 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "f7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.add_column(sa.Column("has_more", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.drop_column("has_more")
