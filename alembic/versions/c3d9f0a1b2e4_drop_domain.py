"""drop domain columns and delete subscriptions

Revision ID: c3d9f0a1b2e4
Revises: 8c2a1b4e7d90
Create Date: 2026-08-14 12:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c3d9f0a1b2e4"
down_revision: Union[str, Sequence[str], None] = "8c2a1b4e7d90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("DELETE FROM subscription_deliveries"))
    op.execute(sa.text("DELETE FROM subscriptions"))
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.drop_constraint("ck_searches_domain", type_="check")
        batch_op.drop_column("domain")
    with op.batch_alter_table("subscriptions", schema=None) as batch_op:
        batch_op.drop_constraint("ck_subscriptions_domain", type_="check")
        batch_op.drop_constraint("uq_subscriptions_user_query", type_="unique")
        batch_op.drop_column("domain")
        batch_op.create_unique_constraint(
            "uq_subscriptions_user_query",
            ["user_id", "query_text"],
        )


def downgrade() -> None:
    with op.batch_alter_table("subscriptions", schema=None) as batch_op:
        batch_op.drop_constraint("uq_subscriptions_user_query", type_="unique")
        batch_op.add_column(sa.Column("domain", sa.Text(), nullable=False, server_default="other"))
        batch_op.create_check_constraint(
            "ck_subscriptions_domain",
            "domain IN ('sport', 'medicine', 'other')",
        )
        batch_op.create_unique_constraint(
            "uq_subscriptions_user_query",
            ["user_id", "domain", "query_text"],
        )
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.add_column(sa.Column("domain", sa.Text(), nullable=False, server_default="other"))
        batch_op.create_check_constraint(
            "ck_searches_domain",
            "domain IN ('sport', 'medicine', 'other')",
        )
