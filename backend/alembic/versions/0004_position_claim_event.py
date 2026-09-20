"""position_claim_event — append-only claim log (#80 spin-off)

Attribution of broker positions the cockpit didn't originate. Append-only: a mutable row + revoked_at can't
represent shrink deficits, corporate-action adjustments, or correction history. Effective quantity is NOT
stored — it is derived per read against the current claimable native position.

Revision ID: 0004_position_claim_event
Revises: 0003_command_ledger
Create Date: 2026-07-28
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0004_position_claim_event"
down_revision: Union[str, None] = "0003_command_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "position_claim_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(length=12), nullable=False),
        sa.Column("claim_id", sa.String(length=64), nullable=False),
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("source_strategy_id", sa.String(length=64), nullable=False),
        sa.Column("source_position_id", sa.String(length=96), nullable=False),
        sa.Column("source_side", sa.String(length=8), nullable=False),
        sa.Column("target_strategy_id", sa.String(length=64), nullable=False),
        # Numeric, not float — these back a book-keeping invariant, not a display value.
        sa.Column("claimed_qty", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("source_avg_px", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("attribution_start_px", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("mark_ts", sa.BigInteger(), nullable=False),
        sa.Column("source_ts_last", sa.BigInteger(), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    # Transport idempotency: a redelivered claim command must not double-book. This constraint — not the
    # pre-insert check — is the real guard when two writers race.
    op.create_unique_constraint("uq_position_claim_event_command_id", "position_claim_event", ["command_id"])
    op.create_index("ix_position_claim_event_claim_id", "position_claim_event", ["claim_id"])
    op.create_index("ix_position_claim_event_account_id", "position_claim_event", ["account_id"])
    op.create_index("ix_position_claim_event_instrument_id", "position_claim_event", ["instrument_id"])
    op.create_index(
        "ix_position_claim_event_target_strategy_id", "position_claim_event", ["target_strategy_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_position_claim_event_target_strategy_id", table_name="position_claim_event")
    op.drop_index("ix_position_claim_event_instrument_id", table_name="position_claim_event")
    op.drop_index("ix_position_claim_event_account_id", table_name="position_claim_event")
    op.drop_index("ix_position_claim_event_claim_id", table_name="position_claim_event")
    op.drop_constraint("uq_position_claim_event_command_id", "position_claim_event", type_="unique")
    op.drop_table("position_claim_event")
