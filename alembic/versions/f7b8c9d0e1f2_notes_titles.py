"""add nullable notes.title_en and notes.title_ru for list snapshots

Revision ID: f7b8c9d0e1f2
Revises: e6a7b8c9d0e1
Create Date: 2026-08-14 15:20:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "e6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("notes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("title_en", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("title_ru", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("notes", schema=None) as batch_op:
        batch_op.drop_column("title_ru")
        batch_op.drop_column("title_en")
