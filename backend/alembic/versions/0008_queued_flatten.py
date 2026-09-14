"""queued_flatten — flatten requests deferred to the next regular-hours open (#170 spin-off)

Outside RTH there is no reliable two-sided quote to price an exit into (the feed goes idle, and a thin
extended-hours book routinely has a 10%+ spread), so a flatten confirmed off-hours is queued instead of
fought into a bad price. The engine replays it as a plain market order the moment the session becomes
regular, through the SAME validation the immediate path uses — nothing new to trust.

Revision ID: 0008_queued_flatten
Revises: 0007_position_transfer_event
Create Date: 2026-07-30
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0008_queued_flatten"
down_revision: Union[str, None] = "0007_position_transfer_event"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "queued_flatten",
        sa.Column("command_id", sa.String(length=64), primary_key=True),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        # What the operator SAW at confirm time — replayed at the open through the same live-state checks
        # the immediate path uses, so a position that moved in the meantime is rejected, not reversed.
        sa.Column("expected_side", sa.String(length=8), nullable=False),
        sa.Column("expected_qty", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),  # QUEUED | SUBMITTING | SUBMITTED | REJECTED
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


def downgrade() -> None:
    op.drop_index("ix_queued_flatten_instrument_id", table_name="queued_flatten")
    op.drop_index("ix_queued_flatten_status", table_name="queued_flatten")
    op.drop_table("queued_flatten")
