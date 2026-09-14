"""drop queued_flatten — superseded by manager/manager_event (#55)

0009 migrated every existing row into `manager`/`manager_event` and deliberately left this table in place,
unused, because the old code (which still referenced it) could still be running mid-rollover. That window
is closed: the engine/api built from the code that stops reading/writing `queued_flatten` is deployed
(0009's already-migrated rows are live and dispatching through the new framework), so nothing reads this
table anymore — safe to drop.

Revision ID: 0010_drop_queued_flatten
Revises: 0009_manager_framework
Create Date: 2026-07-30
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0010_drop_queued_flatten"
down_revision: Union[str, None] = "0009_manager_framework"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_queued_flatten_instrument_id", table_name="queued_flatten")
    op.drop_index("ix_queued_flatten_status", table_name="queued_flatten")
    op.drop_table("queued_flatten")


def downgrade() -> None:
    op.create_table(
        "queued_flatten",
        sa.Column("command_id", sa.String(length=64), primary_key=True),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("expected_side", sa.String(length=8), nullable=False),
        sa.Column("expected_qty", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("error", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_queued_flatten_status", "queued_flatten", ["status"])
    op.create_index("ix_queued_flatten_instrument_id", "queued_flatten", ["instrument_id"])
