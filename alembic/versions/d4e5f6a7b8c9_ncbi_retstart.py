"""add searches.ncbi_retstart for ESearch cursor

Revision ID: d4e5f6a7b8c9
Revises: c3d9f0a1b2e4
Create Date: 2026-08-14 13:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d9f0a1b2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("ncbi_retstart", sa.Integer(), nullable=False, server_default="0"),
        )
        batch_op.create_check_constraint(
            "ck_searches_ncbi_retstart",
            "ncbi_retstart >= 0",
        )
    op.execute(sa.text("UPDATE searches SET ncbi_retstart = page * 10"))


def downgrade() -> None:
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.drop_constraint("ck_searches_ncbi_retstart", type_="check")
        batch_op.drop_column("ncbi_retstart")
