"""Per-strategy capital sleeves and the transition transfer log (#320).

Two numbers per strategy, and the gap between them is the instruction. `target` is operator intent and
changes the instant someone decides, moving no capital. `actual` is the sleeve's net asset value and
moves only when a fill actually frees money. That is what makes a transition safe: the recipient can
only deploy capital the donor has genuinely handed over, so the overlap window — donor still holding,
recipient already growing — cannot double the book's exposure.

`sleeve_transfer.fill_id` is UNIQUE, and that constraint is the idempotency guard rather than
bookkeeping. Fills are not delivered exactly once: reconciliation re-reports them and a reconnect
replays them. Applying one twice pushes the donor below its target and the recipient above its
headroom while each application looks individually correct. An in-memory guard forgets across a
restart, which is precisely when reconciliation replays hardest — so it lives here, where a duplicate
insert simply fails.

NOT the same thing as `position_transfer_event` (#80), which moves POSITION OWNERSHIP between
strategies. That changes who natively holds a position; this changes how much capital each may deploy.

Nothing in the trading path reads these yet. Dropping them loses the allocation record and its audit.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_strategy_sleeves"
down_revision: Union[str, None] = "0013_execquality_constraint_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SLEEVE = "strategy_sleeve"
_TRANSFER = "sleeve_transfer"


def upgrade() -> None:
    op.create_table(
        _SLEEVE,
        sa.Column("strategy_id", sa.String(64), primary_key=True),
        # Numeric, not float: these are money. A binary float would drift the conservation invariant
        # the whole design rests on.
        sa.Column("target", sa.Numeric(28, 10), nullable=False, server_default="0"),
        sa.Column("actual", sa.Numeric(28, 10), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_table(
        _TRANSFER,
        sa.Column("id", sa.Integer, primary_key=True),
        # UNIQUE is the idempotency guard — see the module docstring.
        sa.Column("fill_id", sa.String(96), nullable=False, unique=True, index=True),
        sa.Column("from_strategy", sa.String(64), nullable=False, index=True),
        # May be 'UNALLOCATED': capital that has left the strategies but not the account.
        sa.Column("to_strategy", sa.String(64), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(28, 10), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table(_TRANSFER)
    op.drop_table(_SLEEVE)
