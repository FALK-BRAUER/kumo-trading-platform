"""The observation table's schema is the guarantee, not a convention (#734).

WHAT THE PARTIAL UNIQUE INDEX IS FOR. `Δunrealized(W, lane)` is measured from a window's BASE row, and
there must be exactly one base per lane-instrument-day. If two could exist, "the base" becomes a
read-time choice — and a selection predicate evaluated in two places drifts, which is a class that has
bitten this repo three times (leash validation, manager-armed derivation, percent→bps rounding).

The database is the only place that can decide it atomically, and the same reasoning is written into
migration 0015 for `exec_action_log`'s one-decision-per-slot index.

WHY INTRADAY ROWS ARE EXCLUDED FROM IT. They are observations, not bases. A missing close row must be
a REFUSED base — an em dash until backfill supplies one — never "the nearest intraday row", because
an intraday mark is a live quote and a base must be a close. So intraday captures repeat freely and
can never satisfy the constraint.

VERIFIED AGAINST A REAL POSTGRES, not against SQLAlchemy's opinion of one: partial indexes are a
Postgres feature and SQLite would silently accept a schema that does not enforce this. Migration 0017
was applied to an empty database and all four properties below were exercised there before this test
existed. The first attempt proved nothing — a failed second INSERT aborted its transaction and rolled
the first one back, so the "must be refused" case ran against an EMPTY table and passed for the wrong
reason. Hence the fixture-property assertion in every case here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0017_eod_position_observation.py"
_MODELS = Path(__file__).resolve().parents[1] / "db" / "models.py"



def _column_names() -> set[str]:
    """The DECLARED columns of `EodPositionObservation`, read off the model rather than off prose.

    Asserted non-empty by its own caller below: a parser that resolves nothing reports a clean model
    and a broken matcher identically, which is the failure this file exists to prevent elsewhere.
    """
    from api.db.models import EodPositionObservation

    names = {c.name for c in EodPositionObservation.__table__.columns}
    assert len(names) > 10, f"only resolved {len(names)} columns — the reader is broken, not the model"
    return names


@pytest.fixture(scope="module")
def migration() -> str:
    return _MIGRATION.read_text()


def test_the_scan_reads_the_real_migration(migration: str):
    """VACUITY GUARD. Every assertion below is a substring check; if the file moved or was renamed,
    they would all pass against an empty string and report a healthy schema."""
    assert len(migration) > 2000
    assert "eod_position_observation" in migration
    assert 'revision: str = "0017_eod_position_observation"' in migration


def test_exactly_one_BASE_row_per_lane_instrument_day_is_enforced_by_the_DATABASE(migration: str):
    """Not by a read-time rule. Exercised against a real Postgres: a second `close` row for the same
    (date, lane, instrument) is refused with `duplicate key value violates unique constraint
    "uq_eod_observation_base"`."""
    assert "uq_eod_observation_base" in migration
    idx = migration[migration.index('"uq_eod_observation_base"'):]
    assert "unique=True" in idx[:400]
    assert '"session_date", "strategy_id", "instrument_id"' in idx[:400]


def test_a_RECONSTRUCTED_row_cannot_shadow_a_close_row(migration: str):
    """Both kinds are bases, so both are inside the predicate — a backfill can FILL a missing day and
    can never quietly supersede a day the engine actually observed. Verified live: with a committed
    `close` row present, inserting `reconstructed-eod` for the same key is refused."""
    assert "'close', 'reconstructed-eod'" in migration


def test_INTRADAY_rows_are_NOT_bases_and_may_repeat(migration: str):
    """They are observations. The predicate names the two base kinds explicitly rather than excluding
    intraday by pattern, so a new capture kind is a base only if someone says so — absence of a rule
    must not read as permission. Verified live: two intraday rows for one key both insert."""
    where = re.search(r"_BASE_KINDS = \"([^\"]+)\"", migration)
    assert where, "the base predicate must be a named constant, not inlined twice"
    assert "intraday" not in where.group(1)


def test_the_manifest_exists_so_a_FLAT_lane_is_not_a_MISSING_lane(migration: str):
    """A lane holding nothing must say "observed, 0 positions". Without the manifest, a flat lane and
    a capture that never ran are the same absence — this repo's most-repeated defect class."""
    assert "eod_observation_manifest" in migration
    # NOT `uq_eod_manifest_base` any more — that index was removed deliberately, because a unique
    # (date, lane) key froze a day's outcome and made a failed capture unrepairable. See the
    # attempt-log test below.
    assert "ix_eod_manifest_lookup" in migration
    assert "instrument_count" in migration


def test_no_column_stores_a_CONCLUSION():
    """The whole point of the shape. `basis_contested` (#370) is derived from two raw bases at read
    time; a stored flag is a frozen wrong answer waiting for the rule to change. Same for the Δ
    itself, which is never a column.

    Aimed at the CLASS: any future column matching these names is the same mistake."""
    # COLUMN DECLARATIONS ONLY, not prose. The first version scanned the whole class body and fired on
    # the model's own docstring, which names `basis_contested` precisely to explain why it is NOT a
    # column — a guard that could not tell a warning from the thing it warns about.
    declared = _column_names()
    for banned in ("basis_contested", "delta_unrealized", "unrealized_delta", "is_session_close"):
        assert banned not in declared, (
            f"`{banned}` is a CONCLUSION. Store the inputs it is derived from — a stored conclusion "
            f"cannot be re-derived when the rule changes, and has nothing to disagree with.")


def test_the_venue_columns_exist_or_this_IS_a_parallel_ledger():
    """Rows written from the engine's own cache observe the ENGINE, not reality — an engine defect
    would be faithfully logged as truth. These columns are what give every row its own
    engine-vs-broker diff, and without them the table is the thing CLAUDE.md forbids."""
    declared = _column_names()
    for required in ("qty_venue", "avg_px_venue", "unrealized_venue"):
        assert required in declared, f"`{required}` missing — the table would observe only itself"


def test_session_fill_qty_exists_or_a_SPLIT_is_indistinguishable_from_a_TRADE():
    """`qty_t == qty_{t-1} + session_fill_qty_t + transfers_t` is what makes a corporate action
    mechanically detectable. Without this column a qty discontinuity is ambiguous with an ordinary
    fill, and "splits show up" is a human squinting at a table."""
    assert "session_fill_qty" in _column_names()


# ==================================================================================================
# THE MODEL AND THE MIGRATION ARE TWO DECLARATIONS OF ONE SCHEMA (#734 review, F5)
#
# The tests above read the INDEX from the migration text and the model only for column names — so
# stripping `unique=True` AND the partial predicate from the MODEL left all 46 tests green. Nothing
# create_all's this metadata today (alembic owns the schema), so it is declaration drift rather than
# a live defect; but migration 0015 carries a comment about exactly this hazard, and an unguarded
# second declaration is how the two stop meaning the same thing.
# ==================================================================================================
def _model_index(name: str):
    from api.db.models import EodObservationManifest, EodPositionObservation

    for model in (EodPositionObservation, EodObservationManifest):
        for idx in model.__table__.indexes:
            if idx.name == name:
                return idx
    return None


def test_the_MODEL_declares_the_base_index_the_MIGRATION_creates(migration: str):
    """Unique, partial, and on the same columns — checked against the model's own metadata rather
    than against its source text, so a rename or a reordering cannot slip through."""
    idx = _model_index("uq_eod_observation_base")
    assert idx is not None, "the model no longer declares the base index at all"
    assert idx.unique is True, "the model's base index is not UNIQUE — the migration's is"
    cols = [c.name for c in idx.columns]
    assert cols == ["session_date", "strategy_id", "instrument_id", "method_version"], cols
    # The partial predicate is what makes intraday rows non-bases; without it the model would claim
    # every capture is a base.
    # `str(clause or "")` raises — a SQLAlchemy clause has no boolean value. Stringify it directly.
    where_clause = idx.dialect_options["postgresql"].get("where")
    where = "" if where_clause is None else str(where_clause)
    assert "close" in where and "reconstructed-eod" in where and "intraday" not in where

    # AND THE MIGRATION AGREES on the same four columns, in the same order.
    hunk = migration[migration.index('"uq_eod_observation_base"'):]
    assert '"session_date", "strategy_id", "instrument_id", "method_version"' in hunk[:400]


def test_METHOD_VERSION_is_in_the_base_key_or_the_documented_RERUN_is_impossible(migration: str):
    """THE DEFECT THIS TEST EXISTS FOR, proven on a real Postgres in review.

    `method_version`'s own comment promises a corrected re-derivation "writes rows under a NEW
    version and never updates in place". With the key on (date, lane, instrument) alone, that rerun
    is REFUSED by this very index — a schema enforcing the opposite of the guarantee written beside
    it, with both statements in the same commit.
    """
    idx = _model_index("uq_eod_observation_base")
    assert "method_version" in [c.name for c in idx.columns], (
        "a corrected re-derivation cannot be written without this, and the model promises it can"
    )


def test_the_MANIFEST_is_an_ATTEMPT_log_and_not_one_outcome_per_day(migration: str):
    """Also proven on real Postgres: with a unique (date, lane) index, a close capture that FAILED at
    16:20 could not be repaired by a successful retry at 16:30 — the day stayed frozen as `failed`
    while the observation rows beside it were correct. Two sources disagreeing, manufactured by the
    table meant to prevent that."""
    assert _model_index("uq_eod_manifest_base") is None, (
        "a unique manifest index freezes a day's outcome — a failure can never be followed by a repair"
    )
    assert "uq_eod_manifest_base" not in migration
    assert "ix_eod_manifest_lookup" in migration


def test_no_DEFAULT_manufactures_the_reassuring_answer():
    """A default argument is what makes a missing argument invisible. Each of these previously
    defaulted to the most flattering possible value: "100% attributed", "this lane traded nothing",
    "USD" on an account the code comments say is SGD, "held nothing"."""
    from api.db.models import EodObservationManifest, EodPositionObservation

    for model, column in (
        (EodPositionObservation, "attribution_coverage"),
        (EodPositionObservation, "session_fill_qty"),
        (EodPositionObservation, "currency"),
        (EodObservationManifest, "instrument_count"),
    ):
        col = model.__table__.columns[column]
        assert col.default is None and col.server_default is None, (
            f"{column} has a default; a writer that forgets it would record a plausible lie"
        )
