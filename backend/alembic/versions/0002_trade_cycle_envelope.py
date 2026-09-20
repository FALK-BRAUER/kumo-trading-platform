"""trade_cycle envelope — durable cycle metadata for restart rebuild (#74)

Engine-owned domain metadata (NOT native trade state, NOT a P&L ledger). Stores the cycle boundary + lifecycle
Nautilus can't express, so the engine reseeds the TradeCycleProjection identically after restart.

Revision ID: 0002_trade_cycle_envelope
Revises: 0001_watchlist_item
Create Date: 2026-07-12
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0002_trade_cycle_envelope"
down_revision: Union[str, None] = "0001_watchlist_item"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_cycle",
        sa.Column("cycle_id", sa.String(length=280), primary_key=True),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("opened_ts", sa.BigInteger(), nullable=False),
        sa.Column("closed_ts", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(length=8), nullable=False),
        sa.Column("close_reason", sa.String(length=24), nullable=True),
        sa.Column("close_reason_source", sa.String(length=12), nullable=True),
        sa.Column("import_status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("last_event_ts", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_trade_cycle_account_id", "trade_cycle", ["account_id"])
    op.create_index("ix_trade_cycle_instrument_id", "trade_cycle", ["instrument_id"])
    op.create_index("ix_trade_cycle_strategy_id", "trade_cycle", ["strategy_id"])
    # At most ONE live cycle per canonical key — the projection's active map is keyed by this tuple, so two live
    # rows would make restart seeding order-dependent. Partial: terminal (CLOSED / closed) rows are exempt so a
    # reopened cycle can coexist with the prior closed one.
    op.create_index(
        "uq_trade_cycle_active_key",
        "trade_cycle",
        ["account_id", "client_id", "instrument_id", "strategy_id"],
        unique=True,
        postgresql_where=sa.text("closed_ts IS NULL AND state <> 'CLOSED'"),
    )


def downgrade() -> None:
    op.drop_index("uq_trade_cycle_active_key", table_name="trade_cycle")
    op.drop_index("ix_trade_cycle_strategy_id", table_name="trade_cycle")
    op.drop_index("ix_trade_cycle_instrument_id", table_name="trade_cycle")
    op.drop_index("ix_trade_cycle_account_id", table_name="trade_cycle")
    op.drop_table("trade_cycle")
