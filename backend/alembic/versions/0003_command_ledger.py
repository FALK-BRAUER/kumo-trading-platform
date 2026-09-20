"""command_ledger — durable command idempotency ledger (#78)

Engine-owned. RESERVE-before-act so a restart + re-delivered ui:commands entry can't double-send an order.

Revision ID: 0003_command_ledger
Revises: 0002_trade_cycle_envelope
Create Date: 2026-07-12
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0003_command_ledger"
down_revision: Union[str, None] = "0002_trade_cycle_envelope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "command_ledger",
        sa.Column("command_id", sa.String(length=64), primary_key=True),
        sa.Column("command_type", sa.String(length=24), nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("error", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # Economic idempotency — at most one order per client_order_id (unique when present).
    op.create_index(
        "uq_command_ledger_client_order_id", "command_ledger", ["client_order_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_command_ledger_client_order_id", table_name="command_ledger")
    op.drop_table("command_ledger")
