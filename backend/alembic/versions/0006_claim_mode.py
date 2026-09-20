"""position_claim_event — claim_mode NATIVE|PROJECTION (#80 spin-off, phase 6)

NATIVE claims are handed to Nautilus's own `external_order_claims`: at reconciliation the position is
attributed to the claiming strategy instead of EXTERNAL, so it becomes a real native position — fully
manageable, P&L native-derived, no projection. Whole-instrument only (that is what Nautilus supports), and
it takes effect at engine start when strategies register their claims.

PROJECTION claims stay attribution-only, for partial quantities Nautilus cannot express.

Revision ID: 0006_claim_mode
Revises: 0005_claim_partial_events
Create Date: 2026-07-28
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0006_claim_mode"
down_revision: Union[str, None] = "0005_claim_partial_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows were all attribution-only — default them to PROJECTION so no claim silently becomes native.
    op.add_column(
        "position_claim_event",
        sa.Column("claim_mode", sa.String(length=12), nullable=False, server_default="PROJECTION"),
    )


def downgrade() -> None:
    op.drop_column("position_claim_event", "claim_mode")
