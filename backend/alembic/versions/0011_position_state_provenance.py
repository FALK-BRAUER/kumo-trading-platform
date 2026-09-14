"""Trail provenance on exec_position_state (#197 B1 / P1).

The give-back trail went dead in production because a position with no state row was seeded
`entry, peak = (today's price, today's price)` — asserting a peak that had never happened. On 6 Aug
2026 that left the rule unarmed on seven positions (peak == entry, so peak_gain == 0 and it cannot
fire) and firing on a 0.06% rounding artefact for the eighth. Measured cost on the six closed
positions was material, and no exit rule could have prevented it because the trail was fiction.

The fix needs the trail to say HOW MUCH IT KNOWS, so the evaluator can skip peak-relative rules on a
position whose peak was never observed rather than inventing one:

  quality               'live' (opened while we watched), 'reconstructed' (entry from the real fill,
                        peak rebuilt from bars covering the hold), or 'adopted' (peak NOT known).
  sessions_held         needed by max_hold_days, which counts SESSIONS not calendar days.
  sessions_since_high   needed by stall_days.

New rows default to **'adopted'**: written by the old seeding path, their peak is exactly the thing
that cannot be trusted, and defaulting to 'live' would preserve the bug for the positions that have
it.

Existing rows are then backfilled on one observable fact: **the old bug always wrote peak == entry**,
so a row with `peak > entry` recorded a high that was actually observed above its entry, and marking
it 'adopted' would disarm give-back on precisely the rows whose trail IS trustworthy. On 9 Aug 2026
that is both live rows (MET 94.85/99.95, PRU 122.63/123.54, entries rebuilt from the real fills), and
a blanket 'adopted' would have silently changed live exit behaviour for both.

`sessions_held` and `sessions_since_high` start at 0 for existing rows because nothing in this table
records when the position opened. That under-counts a position held for weeks, so `max_hold_days` and
`stall_days` fire late rather than early — the direction that holds a position too long instead of
liquidating one on migration day.

Nullable / defaulted throughout so the strategy package can deploy before or after this migration
without a window where it selects a column that does not exist.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_position_state_provenance"
down_revision: Union[str, None] = "0010_drop_queued_flatten"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "exec_position_state"


def _table_exists() -> bool:
    """Does `exec_position_state` exist yet?

    IT IS NOT OURS. `exec_position_state` is declared by kumo-strategies
    (`runtime/executor/store.py`) and created by its `create_all`, NOT by any migration in this repo —
    grep confirms no `op.create_table` for it anywhere in `alembic/versions/`. So on a database that
    already has the table this migration ADDS COLUMNS, and on a fresh one there is nothing to add
    because `create_all` builds it from a model that already declares all three.

    THIS WAS FOUND BY BOOTSTRAPPING A SECOND INSTANCE, and it had never been reachable before:

        sqlalchemy.exc.ProgrammingError: relation "exec_position_state" does not exist
        [SQL: ALTER TABLE exec_position_state ADD COLUMN sessions_held INTEGER ...]

    kumo-paper's database grew incrementally over months, so the table was always present by the time
    this ran. An EMPTY database had never been tried, and the migration chain therefore encoded an
    ordering dependency on another repo that nothing asserted. The api crash-looped.
    """
    return sa.inspect(op.get_bind()).has_table(_TABLE)


def upgrade() -> None:
    if not _table_exists():
        # Fresh database: kumo-strategies' `create_all` will build the table WITH these columns,
        # because its model declares them. Skipping is correct here and silence is not — a migration
        # that no-ops for the right reason and a migration that no-ops because it is broken look
        # identical afterwards.
        op.execute("SELECT 1")  # keeps the revision in alembic_version
        print(f"alembic 0011: {_TABLE} does not exist yet — kumo-strategies' create_all owns it and "
              f"its model already declares sessions_held / sessions_since_high / quality. Nothing to "
              f"add on a fresh database.")
        return
    op.add_column(_TABLE, sa.Column("sessions_held", sa.Integer(), nullable=False,
                                    server_default="0"))
    op.add_column(_TABLE, sa.Column("sessions_since_high", sa.Integer(), nullable=False,
                                    server_default="0"))
    op.add_column(_TABLE, sa.Column("quality", sa.String(length=16), nullable=False,
                                    server_default="adopted"))
    # See the module docstring: a peak strictly above entry cannot have come from the seeding bug,
    # which always wrote them equal. Those rows keep their peak-relative exits.
    op.execute(f"UPDATE {_TABLE} SET quality = 'reconstructed' WHERE peak > entry")


def downgrade() -> None:
    # Order matters only for readability; none of these carry constraints.
    op.drop_column(_TABLE, "quality")
    op.drop_column(_TABLE, "sessions_since_high")
    op.drop_column(_TABLE, "sessions_held")
