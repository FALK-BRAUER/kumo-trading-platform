"""watchlist_item — first app-data table (runtime watchlist)

Revision ID: 0001_watchlist_item
Revises:
Create Date: 2026-07-07
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0001_watchlist_item"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "watchlist_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_watchlist_item_instrument_id", "watchlist_item", ["instrument_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_watchlist_item_instrument_id", table_name="watchlist_item")
    op.drop_table("watchlist_item")
