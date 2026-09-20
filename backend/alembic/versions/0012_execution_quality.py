"""Execution-quality legs (#210): drift between our fills and the official opening auction.

The backtest fills at the daily open — the 09:30:00 auction print. The live runner submits at 09:35
by configuration and fills 09:35-09:36. They price the same decision at two different moments, by
construction, and the first three sessions measured +35.7 bps of drift at t=1.32: an estimate that
establishes nothing, because 22 legs clustered in three mornings is effectively n=3.

Settling it needs 30-60 trading days, which only happens if the number is recorded as it goes. Hence
a table rather than a one-off script: the sample is meant to be grouped and re-analysed as it grows.

UNIQUE on (session, symbol, side) so the recorder is idempotent — a job that re-runs a day adds
nothing and corrects nothing.

Nothing in the trading path reads or writes this. Dropping it would cost a research sample.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_execution_quality"
down_revision: Union[str, None] = "0011_position_state_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "execution_quality_leg"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session", sa.String(length=10), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("qty", sa.Numeric(20, 8), nullable=False),
        sa.Column("fill_vwap", sa.Numeric(20, 8), nullable=False),
        sa.Column("auction_px", sa.Numeric(20, 8), nullable=False),
        sa.Column("auction_exchange", sa.String(length=8), nullable=False),
        sa.Column("auction_size", sa.Numeric(20, 4), nullable=False),
        sa.Column("first_fill_utc", sa.String(length=40), nullable=False),
        sa.Column("lag_minutes", sa.Numeric(10, 3), nullable=False),
        sa.Column("drift_bps", sa.Numeric(12, 3), nullable=False),
        sa.Column("cost_usd", sa.Numeric(16, 4), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        comment="drift between our fills and the official opening auction (#210)",
    )
    op.create_index(f"ix_{_TABLE}_session", _TABLE, ["session"])
    op.create_unique_constraint(f"uq_{_TABLE}_leg", _TABLE, ["session", "symbol", "side"])


def downgrade() -> None:
    op.drop_table(_TABLE)
