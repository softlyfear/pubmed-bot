"""clear query_translation_cache so old DeepL-only text_en cannot skip rewriter

Revision ID: e6a7b8c9d0e1
Revises: d4e5f6a7b8c9
Create Date: 2026-08-14 13:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("DELETE FROM query_translation_cache"))


def downgrade() -> None:
    pass
