"""position_claim_event — partial CONSUMED/REVOKED events + fill identity (#80 spin-off, phase 6)

A claim is no longer all-or-nothing: quantity can be partially consumed by an exit or partially revoked, so
remaining quantity is folded from the event stream. Consumption is keyed on broker fill identity — a cache
delta can't be observed reliably (Nautilus updates position state before publishing OrderFilled) and would be
lost entirely on a crash between fill and write.

Revision ID: 0005_claim_partial_events
Revises: 0004_position_claim_event
Create Date: 2026-07-28
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0005_claim_partial_events"
down_revision: Union[str, None] = "0004_position_claim_event"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("position_claim_event", sa.Column("fill_client_order_id", sa.String(length=64), nullable=True))
    op.add_column("position_claim_event", sa.Column("fill_venue_order_id", sa.String(length=64), nullable=True))
    op.add_column("position_claim_event", sa.Column("fill_trade_id", sa.String(length=64), nullable=True))
    op.add_column("position_claim_event", sa.Column("fill_px", sa.Numeric(precision=28, scale=10), nullable=True))
    # One consumption per (claim, broker trade) — the durable guard against replaying a fill twice.
    op.create_unique_constraint(
        "uq_position_claim_event_claim_trade", "position_claim_event", ["claim_id", "fill_trade_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_position_claim_event_claim_trade", "position_claim_event", type_="unique")
    op.drop_column("position_claim_event", "fill_px")
    op.drop_column("position_claim_event", "fill_trade_id")
    op.drop_column("position_claim_event", "fill_venue_order_id")
    op.drop_column("position_claim_event", "fill_client_order_id")
