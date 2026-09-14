"""`slot` on exec_action_log, so a session can hold more than one decision (kumo-strategies#29).

MOMENTUM-002 decides once a day, at the open plus five minutes. The midday-rotation work needs a
second decision in the same session, and the guarantee that stops two concurrent runs both submitting
is a PARTIAL UNIQUE INDEX on `(strategy_id, session) WHERE kind = 'decision'` — which, unchanged,
would refuse the second slot as a duplicate of the first.

So the index moves to `(strategy_id, session, slot)`. With one slot configured that is IDENTICAL IN
STRENGTH to what it replaces; with several it permits exactly one decision per slot, and a retry of a
slot that already decided is still refused. Weakening it was never an option: a check-then-write in
application code races — two concurrent session runs both read "no decision yet" before either
commits, and both submit orders. The database is the only place that can decide atomically.

`server_default` is `'open+5m'` (`store.DEFAULT_SLOT`) rather than a nullable column, and that choice
does the migration's real work. Existing rows are from the single-slot era and genuinely belong to
that slot, so labelling them is accurate rather than a convenience. A NULLable column would let
NULL-slot rows coexist with slotted ones for the same session, and NULL is never equal to NULL in a
unique index — so two NULL-slot decisions would both be permitted and the guarantee would be silently
gone for exactly the rows that predate the change.

MUST BE APPLIED BEFORE the strategies package writes a slot: `pgjournal.record_decision` passes
`slot=slot or DEFAULT_SLOT` unconditionally, so without the column every decision insert fails.

Cockpit owns the migration; kumo-strategies owns the model (`runtime/executor/store.py`). This mirrors
that model exactly — the index name, the partial predicate and the column type are copied from it, not
invented here, because two declarations of one schema will drift.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0015_exec_action_log_slot"
down_revision: Union[str, None] = "0014_strategy_sleeves"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "exec_action_log"
_INDEX = "uq_exec_one_decision_per_session"
#: `kumo_strategies.runtime.executor.store.DEFAULT_SLOT`. Duplicated as a literal because a migration
#: must describe the schema at THIS revision — importing the constant would make an old migration
#: change meaning when the package moves, which is how a replayed history stops reproducing.
_DEFAULT_SLOT = "open+5m"


def _table_exists() -> bool:
    """Does `exec_action_log` exist yet?

    IT IS NOT OURS — kumo-strategies declares it and its `create_all` builds it; no migration in this
    repo creates it. Same shape as 0011, found the same way: bootstrapping a SECOND instance against
    an empty database, which had never been done. kumo-paper's database grew incrementally, so both
    tables were always present by the time these ran.
    """
    return sa.inspect(op.get_bind()).has_table(_TABLE)


def upgrade() -> None:
    if not _table_exists():
        # Fresh database: kumo-strategies' create_all builds it from a model that already declares
        # `slot`. Announced rather than silent — a no-op for the right reason and a no-op because
        # something is broken look identical afterwards.
        print(f"alembic 0015: {_TABLE} does not exist yet — kumo-strategies' create_all owns it and "
              f"its model already declares `slot`. Nothing to add on a fresh database.")
        return
    op.add_column(
        _TABLE,
        sa.Column("slot", sa.String(32), nullable=False, server_default=_DEFAULT_SLOT),
    )
    # Drop BEFORE creating: the replacement carries the same name, and the old two-column index would
    # otherwise still be enforcing the constraint this migration exists to relax.
    op.drop_index(_INDEX, table_name=_TABLE)
    op.create_index(
        _INDEX, _TABLE, ["strategy_id", "session", "slot"],
        unique=True, postgresql_where=text("kind = 'decision'"),
    )


def downgrade() -> None:
    # Restoring the two-column index can FAIL, and that is correct rather than unfortunate: if more
    # than one slot has decided in a session, those rows are legitimately duplicates under the old
    # constraint and there is no honest way to choose which survives. A downgrade that silently
    # deleted decisions would destroy the record of orders that were actually sent.
    op.drop_index(_INDEX, table_name=_TABLE)
    op.create_index(
        _INDEX, _TABLE, ["strategy_id", "session"],
        unique=True, postgresql_where=text("kind = 'decision'"),
    )
    op.drop_column(_TABLE, "slot")
