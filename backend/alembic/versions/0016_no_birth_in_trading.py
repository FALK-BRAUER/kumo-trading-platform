"""A strategy row may not be CREATED already trading (#443).

WHAT HAPPENED. On 2026-08-21 TECHIVOL-005 was put live by a psql INSERT with `state='TRADING'` — by
Claude, at the operator's request. It then formed eight liquidation orders against other strategies'
positions. The lifecycle state machine lives in Python in kumo-strategies and was never consulted,
because nothing on that path goes through it: cockpit only ever READS this table, and kumo-strategies
never writes it. The rows are written by hand.

THIS IS AN AUDIT PROPERTY, NOT A CONTROL, and it must be defended as one. It does not prevent anything
— `INSERT ... 'DISABLED'` followed by `UPDATE ... 'TRADING'` is two statements instead of one, and no
trigger will ever stop a determined operator. What it buys is the thing that actually failed:

    A ROW THAT APPEARS HAS NO TRANSITION, AND THEREFORE NO JOURNAL EVENT.

The journal holds nineteen `-> TRADING` state rows. TECHIVOL's promotion produced none of them,
because there was nothing to observe — no prior state, nothing for any code path to record or object
to. After this, every trading strategy has been promoted FROM something, so "TRADING with no
journalled promotion" becomes a detectable anomaly instead of being indistinguishable from normal.
Three days of this week went on defects whose entire cost was that nobody could see them.

A guard sold as prevention that only provides evidence is how the next person decides guards are
decoration. So: evidence, deliberately, and that is enough.

WHAT THIS DELIBERATELY DOES NOT DO. An earlier draft also required the prior state to be SHADOW. The operator
rejected SHADOW outright — "a strategy crashing in shadow or live doesn't make a difference" — and the
real gate is now kumo-strategies' boot-time `dry_run`, which runs the full decide->size->form-order
path against a broker seeded with FOREIGN positions and fails at startup before any market data
exists. That catches the eight-foreign-orders bug directly, which SHADOW never would have. Promotion
from ANY state is legal here.

Nor does it re-implement the rest of the machine. A trigger mirroring every edge would duplicate the
Python and the two would drift — the failure this codebase keeps paying for.

THE ERROR TEACHES THE FIX. It fails at 3am, in psql, for someone bootstrapping an environment who has
never read this file, so the message carries the two statements that work. MESSAGE/DETAIL/HINT are
separate because psql prints all three and truncating one still leaves the others.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_no_birth_in_trading"
down_revision: Union[str, None] = "0015_exec_action_log_slot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FN = "refuse_birth_in_trading"
_TRIGGER = "trg_no_birth_in_trading"
_TABLE = "exec_strategy_state"


def _table_exists() -> bool:
    """Does `exec_strategy_state` exist yet?

    THIRD instance of the same shape, and the one that showed my own guard was too narrow. 0011 and
    0015 ALTER a foreign table and were caught by scanning for `add_column`/`alter_column`. This one
    creates a TRIGGER through `op.execute(...)` — raw SQL, invisible to that scan — so it sailed
    through and crash-looped the api on the next boot:

        relation "exec_strategy_state" does not exist
        [SQL: the trigger creation below, against a table that is not there yet]

    NOTE ON THE WORDING: this comment deliberately does NOT quote the trigger-creation statement
    verbatim. `test_no_birth_in_trading.py` extracts it with a non-greedy regex from the FIRST
    occurrence in this file, so a quoted copy in prose becomes the match and the test then reads the
    error-message SQL below it — which contains the word UPDATE and fails an assertion about the
    trigger firing on INSERT only. Prose that quotes code changes what a source-matching test sees.

    Aiming at a class is only as good as the definition of the class. `op.execute` was the door left
    open.
    """
    return sa.inspect(op.get_bind()).has_table(_TABLE)


def upgrade() -> None:
    if not _table_exists():
        # Fresh database: kumo-strategies' create_all builds `exec_strategy_state`. The trigger is
        # applied by a later boot, once the table exists — announced rather than silent, because a
        # no-op for the right reason and a no-op because something is broken look identical after.
        print(f"alembic 0016: {_TABLE} does not exist yet — kumo-strategies' create_all owns it. "
              f"The birth-in-TRADING trigger is not installed on this database yet.")
        return
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {_FN}() RETURNS trigger AS $$
        BEGIN
            IF NEW.state = 'TRADING' THEN
                RAISE EXCEPTION
                    'strategy % cannot be CREATED in TRADING', NEW.strategy_id
                USING
                    DETAIL =
                        'A row inserted already trading has no transition, so no promotion is ever '
                        'journalled and the strategy is live with nothing on the record. That is how '
                        'TECHIVOL-005 went live on 2026-08-21 and formed 8 orders against other '
                        'strategies'' positions. This is an audit rule, not a lock: two statements do '
                        'what one did, and that is the point — the second one is an EVENT.',
                    HINT =
                        'Create it stopped, then promote it. Both statements, in order: '
                        'INSERT INTO exec_strategy_state (strategy_id, state, reason) VALUES '
                        '(''' || NEW.strategy_id || ''', ''DISABLED'', ''registered by <who>, <why>''); '
                        'UPDATE exec_strategy_state SET state = ''TRADING'', reason = '
                        '''operator: <who>, <why>'' WHERE strategy_id = ''' || NEW.strategy_id || '''; '
                        'The reason text is what the next person reads at 3am — write a sentence, not a word. '
                        'See kumo-cockpit#443.';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    # INSERT ONLY. An UPDATE into TRADING is a promotion and is exactly what this wants to encourage;
    # constraining which state it comes FROM is the SHADOW mandate that was cut. TRADING -> TRADING is
    # untouched too, so the engine's boot-time upsert of an already-live strategy cannot be broken by
    # this — a failure there would take the whole node down on restart, a far larger blast radius than
    # the thing being guarded.
    op.execute(f"""
        CREATE TRIGGER {_TRIGGER}
        BEFORE INSERT ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {_FN}();
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON {_TABLE};")
    op.execute(f"DROP FUNCTION IF EXISTS {_FN}();")
