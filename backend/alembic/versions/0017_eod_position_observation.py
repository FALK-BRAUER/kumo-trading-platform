"""`eod_position_observation` + its manifest — what the book looked like at a capture (#734).

WHY A TABLE AT ALL. A lane's window P&L should be `realized(W) + Δunrealized(W)`, the identity the
account hero already uses. `Δunrealized(W, lane)` needs the lane's holdings and their basis at the
window's START, and the broker publishes ACCOUNT-level equity curves only — there is no per-lane mark
at any past instant to ask for. So it is observed, per position, per day.

WHY COMPONENTS AND NOT THE ANSWER. Operator: "we should not snapshot only one number but rather something
that helps to audit and reverse compute." A stored derived number cannot be audited, cannot be
re-derived when a rule changes, and has nothing to disagree with — which is precisely the condition
under which a severed wire is invisible. So the row carries qty, both raw bases, and the mark, and
Δunrealized is derived at read time.

For the same reason there is no `basis_contested` column. That flag (#370, measured: WHD read
+$263.84 against the broker's +$9.52) is a CONCLUSION; both raw bases are stored and the flag is
derived. A stored conclusion is a frozen wrong answer waiting for the rule to change.

WHY THE `_venue` COLUMNS ARE NOT OPTIONAL. Rows written from the engine's own cache observe the
ENGINE, not reality — an engine defect would be faithfully logged as truth, and the table would be a
second opinion of the first. `qty_venue` / `avg_px_venue` / `unrealized_venue` give every row its own
engine-vs-broker diff. Without them this genuinely is the parallel ledger CLAUDE.md forbids.

WHY THE PARTIAL UNIQUE INDEX. `capture_kind` records WHICH JOB wrote the row — provenance, not a
verdict, the same pattern as `exec_action_log.slot` (0015). Exactly one `close`/`reconstructed-eod`
row may exist per lane-instrument-day, and that row IS the window base. Intraday captures are
observations and are deliberately not eligible: a missing close row must be a REFUSED base — an em
dash until backfill supplies one — never "the nearest intraday row". Enforcing that in the database
rather than in a read-time rule is deliberate; a selection predicate evaluated in two places drifts,
and that class has bitten this repo three times.

WHY A MANIFEST. A lane holding nothing must say "observed, 0 positions". Without it, a flat lane and
a capture that never ran are the same absence, and absence readable as flatness is this repo's
most-repeated defect class. Three states, never two.

NO SERVER DEFAULTS ON THE COLUMNS THAT MATTER. `attribution_coverage`, `session_fill_qty`,
`currency` and `instrument_count` are NOT NULL with no default, so a writer that forgets one FAILS
rather than recording a plausible lie: "100% attributed", "this lane traded nothing", "USD" on an SGD
account, "held nothing". A default argument is what makes a missing argument invisible, and each of
these defaults manufactured exactly the reassuring answer.

NOTHING IS BACKFILLED HERE. The tables arrive empty; reconstruction is a separate, reviewed step with
its own acceptance gate (reconstruct at T=now, demand per-lane equality against the engine's native
per-strategy positions). A migration that also computed history would make a schema change and an
unverified derivation land together.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0017_eod_position_observation"
down_revision: Union[str, None] = "0016_no_birth_in_trading"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OBS = "eod_position_observation"
_MAN = "eod_observation_manifest"

#: The predicate that makes a row a window BASE. Written as a literal because a migration must
#: describe the schema at THIS revision — importing the constant would make an old migration change
#: meaning when the code moves, which is how a replayed history stops reproducing (0015's rule).
_BASE_KINDS = "capture_kind IN ('close', 'reconstructed-eod')"


def upgrade() -> None:
    op.create_table(
        _OBS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_date", sa.String(10), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("instrument_id", sa.String(32), nullable=False),
        sa.Column("capture_kind", sa.String(32), nullable=False),
        sa.Column("snapshot_ts", sa.BigInteger(), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        # Three-state throughout: NULL is "not asked", which is not "agrees".
        sa.Column("avg_px_engine", sa.Float(), nullable=True),
        sa.Column("avg_px_venue", sa.Float(), nullable=True),
        sa.Column("qty_venue", sa.Float(), nullable=True),
        sa.Column("mark_px", sa.Float(), nullable=True),
        sa.Column("mark_source", sa.String(24), nullable=True),
        sa.Column("mark_ts", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("unrealized_engine", sa.Float(), nullable=True),
        sa.Column("unrealized_venue", sa.Float(), nullable=True),
        sa.Column("session_fill_qty", sa.Float(), nullable=False),
        sa.Column("cycle_id", sa.String(280), nullable=True),
        sa.Column("quality", sa.String(16), nullable=True),
        sa.Column("provenance", sa.String(16), nullable=False),
        sa.Column("method_version", sa.String(24), nullable=False),
        sa.Column("attribution_coverage", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(f"ix_{_OBS}_session_date", _OBS, ["session_date"])
    op.create_index(f"ix_{_OBS}_strategy_id", _OBS, ["strategy_id"])
    op.create_index(f"ix_{_OBS}_instrument_id", _OBS, ["instrument_id"])
    # `method_version` IS IN THE KEY. Without it the documented rerun — "a corrected re-derivation
    # writes rows under a NEW version and never updates in place" — is REFUSED by this index, which is
    # a schema contradicting the guarantee written beside it. Caught in review, proven on a real
    # Postgres: a v3 backfill over a v2 row failed with a duplicate-key violation.
    op.create_index(
        "uq_eod_observation_base", _OBS,
        ["session_date", "strategy_id", "instrument_id", "method_version"],
        unique=True, postgresql_where=text(_BASE_KINDS),
    )

    op.create_table(
        _MAN,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_date", sa.String(10), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("capture_kind", sa.String(32), nullable=False),
        sa.Column("snapshot_ts", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("instrument_count", sa.Integer(), nullable=False),
        sa.Column("detail", sa.String(256), nullable=True),
        sa.Column("provenance", sa.String(16), nullable=False),
        sa.Column("method_version", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(f"ix_{_MAN}_session_date", _MAN, ["session_date"])
    op.create_index(f"ix_{_MAN}_strategy_id", _MAN, ["strategy_id"])
    # NOT UNIQUE. A unique (session_date, strategy_id) index froze a day's outcome forever: a close
    # capture that failed at 16:20 could not be repaired by a successful retry at 16:30, so the day
    # stayed `failed` while the observation rows beside it were correct — two sources disagreeing,
    # manufactured by the table meant to prevent exactly that. A failure and a later success are BOTH
    # true; this is an attempt log and the day's status is the latest attempt.
    op.create_index("ix_eod_manifest_lookup", _MAN, ["session_date", "strategy_id", "capture_kind"])


def downgrade() -> None:
    # Safe to drop outright: nothing reads these tables for trading decisions, and every row is an
    # OBSERVATION that reconstruction can regenerate. That is the practical difference between an
    # observation log and a ledger — losing it costs history, never truth.
    op.drop_table(_MAN)
    op.drop_table(_OBS)
