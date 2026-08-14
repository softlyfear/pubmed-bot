"""query_en columns and query_translation_cache

Revision ID: 8c2a1b4e7d90
Revises: 1f9d066a88d7
Create Date: 2026-08-14 11:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "8c2a1b4e7d90"
down_revision: Union[str, Sequence[str], None] = "1f9d066a88d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "query_translation_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("text_en", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint("length(text_en) > 0", name="ck_query_translation_cache_text_en"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_hash", name="uq_query_translation_cache_hash"),
    )
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.add_column(sa.Column("query_en", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE searches SET query_en = query_text WHERE query_en IS NULL"))
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.alter_column("query_en", existing_type=sa.Text(), nullable=False)
        batch_op.create_check_constraint("ck_searches_query_en", "length(query_en) > 0")
    with op.batch_alter_table("subscriptions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("query_en", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE subscriptions SET query_en = query_text WHERE query_en IS NULL"))
    with op.batch_alter_table("subscriptions", schema=None) as batch_op:
        batch_op.alter_column("query_en", existing_type=sa.Text(), nullable=False)
        batch_op.create_check_constraint("ck_subscriptions_query_en", "length(query_en) > 0")


def downgrade() -> None:
    with op.batch_alter_table("subscriptions", schema=None) as batch_op:
        batch_op.drop_constraint("ck_subscriptions_query_en", type_="check")
        batch_op.drop_column("query_en")
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.drop_constraint("ck_searches_query_en", type_="check")
        batch_op.drop_column("query_en")
    op.drop_table("query_translation_cache")
