"""position_transfer_event — internal position transfers between strategies (#80 spin-off)

Replaces the claim model. A claim was attribution floating over a position that never moved; a transfer
actually moves quantity between two strategies with paired internal fills, so the position becomes genuinely
the target's — exitable, cycle-projectable, P&L native-derived.

Append-only with outbox semantics. `ExecutionEngine.process()` is queued and not transactional, so a crash
between the two legs would leave the books split; the event sequence PREPARED → SOURCE_APPLIED →
DEST_APPLIED → COMPLETED lets startup recovery finish or fail an interrupted transfer deterministically.

`position_claim_event` is left in place as audit of the superseded approach — its rows describe attribution
that never moved anything, so they must not be silently reinterpreted as transfers.

Revision ID: 0007_position_transfer_event
Revises: 0006_claim_mode
Create Date: 2026-07-28
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0007_position_transfer_event"
down_revision: Union[str, None] = "0006_claim_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "position_transfer_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # PREPARED | SOURCE_APPLIED | DEST_APPLIED | COMPLETED | FAILED
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("transfer_id", sa.String(length=64), nullable=False),
        sa.Column("command_id", sa.String(length=64), nullable=False),
        # --- canonical identity: instrument alone can't name a position ---
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("source_strategy_id", sa.String(length=64), nullable=False),
        sa.Column("target_strategy_id", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=28, scale=10), nullable=False),
        # MARKET (source crystallizes P&L) | CARRY_OVER (basis migrates, nothing realized)
        sa.Column("pricing_mode", sa.String(length=12), nullable=False),
        sa.Column("transfer_px", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("source_avg_px", sa.Numeric(precision=28, scale=10), nullable=False),
        # Deterministic leg ids — recovery finds a half-applied transfer by looking these up in the cache.
        sa.Column("source_order_id", sa.String(length=96), nullable=False),
        sa.Column("dest_order_id", sa.String(length=96), nullable=False),
        sa.Column("source_ts_last", sa.BigInteger(), nullable=False),
        sa.Column("reversal_of_transfer_id", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("error", sa.String(length=256), nullable=True),
        sa.Column("event_ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    # One event of a given type per transfer — makes replay of any leg idempotent at the database level.
    op.create_unique_constraint(
        "uq_transfer_event_type", "position_transfer_event", ["transfer_id", "event_type"]
    )
    op.create_index("ix_transfer_event_transfer_id", "position_transfer_event", ["transfer_id"])
    op.create_index("ix_transfer_event_command_id", "position_transfer_event", ["command_id"])
    op.create_index("ix_transfer_event_instrument_id", "position_transfer_event", ["instrument_id"])


def downgrade() -> None:
    op.drop_index("ix_transfer_event_instrument_id", table_name="position_transfer_event")
    op.drop_index("ix_transfer_event_command_id", table_name="position_transfer_event")
    op.drop_index("ix_transfer_event_transfer_id", table_name="position_transfer_event")
    op.drop_constraint("uq_transfer_event_type", "position_transfer_event", type_="unique")
    op.drop_table("position_transfer_event")
