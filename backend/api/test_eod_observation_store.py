"""Mapping an observation to rows, and the guarantees the write must keep (#734 step 3).

THE MAPPING IS TESTED OFFLINE, DELIBERATELY. The round-trip needs a live Postgres and is
`needs_services`, which `addopts = "-m 'not engine and not needs_services'"` deselects from every
default run — and this repo has already found that those tests do not merely skip, they ERROR
(no `pytest-asyncio`, no `KUMO_DATABASE_URL`) with nothing reporting it. A mapping that could only be
checked there would be effectively unchecked.

So everything that can be decided without a database is decided here, and the integration test below
covers only what genuinely needs one: the partial unique index actually refusing a duplicate base.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from api.eod_observation_store import (
    BASE_KINDS,
    METHOD_VERSION,
    failed_manifest_row,
    manifest_rows,
    observation_rows,
)
from api.eod_observer import LaneObservation, PositionObservation


def _pos(lane, sym, qty=9.0, basis=100.0, mark=110.0, engine_pnl=90.0):
    return PositionObservation(
        strategy_id=lane, instrument_id=sym, qty=qty, avg_px_engine=basis,
        mark_px=mark, unrealized_derived=None if mark is None else qty * (mark - basis),
        unrealized_engine=engine_pnl,
    )


def dataclasses_replace_coverage(lane, coverage):
    import dataclasses

    return dataclasses.replace(lane, attribution_coverage=coverage)


def _lane(lane, positions, unpriced=0, total=90.0):
    return LaneObservation(
        strategy_id=lane, positions=tuple(positions),
        unrealized_total=None if unpriced else total, unpriced=unpriced,
        # REQUIRED on the dataclass, with no default — 1.0 is a fact for an engine observation and a
        # MEASUREMENT for a reconstruction, and a default would let the second inherit the first's
        # certainty (#734 review, F11).
        attribution_coverage=1.0,
    )


# ==================================================================================================
# TWO DECLARATIONS OF ONE RULE
# ==================================================================================================
def test_the_store_and_the_MIGRATION_agree_on_what_a_base_kind_is():
    """`BASE_KINDS` here and `_BASE_KINDS` in migration 0017 are the same rule written twice — the
    store decides whether to apply the conflict clause, the database decides whether to enforce it.
    If they drift, the store stops guarding rows the database still constrains (or worse, the other
    way round) and nothing fails until a duplicate arrives in production.

    This repo has been bitten by two-derivations-of-one-predicate three times; the fix is always to
    pin that they agree rather than to hope."""
    migration = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0017_eod_position_observation.py"
    ).read_text()
    # FIXTURE PROPERTY: the migration was actually read, or every check below passes vacuously.
    assert "_BASE_KINDS" in migration and len(migration) > 2000

    # EQUALITY, NOT CONTAINMENT — both directions. The first version asserted only that every kind
    # here appears in the migration, which SHRINKING `BASE_KINDS` satisfies: dropping
    # "reconstructed-eod" left all eleven tests green while the store stopped guarding rows the
    # database still constrains. "Non-empty is not complete", caught by mutation.
    predicate = migration.split("_BASE_KINDS = ")[1].split("\n")[0]
    in_migration = set(re.findall(r"'([a-z-]+)'", predicate))
    assert in_migration == set(BASE_KINDS), (
        f"the store says {sorted(BASE_KINDS)} and migration 0017 says {sorted(in_migration)} — "
        f"two declarations of one rule, drifted"
    )
    assert "intraday" not in predicate


# ==================================================================================================
# THE MAPPING
# ==================================================================================================
def test_a_MISSING_venue_reading_is_NULL_and_not_a_zero_and_not_agreement():
    """Three states. NULL means the broker was not asked — which is not the broker agreeing, and is
    certainly not the broker reporting zero. #370's own rule, and the reason `basis_contested` is
    derived from these two columns rather than stored as a verdict."""
    rows = observation_rows("2026-08-29", "close", 1, [_lane("MOMENTUM-002", [_pos("MOMENTUM-002", "AEM.XNYS")])], currency="USD", mark_source="close")
    assert rows[0]["avg_px_venue"] is None
    assert rows[0]["qty_venue"] is None
    assert rows[0]["unrealized_venue"] is None


def test_a_PRESENT_venue_reading_is_carried_so_the_row_holds_its_own_disagreement():
    """Without these columns the table observes the ENGINE, not reality — an engine defect would be
    faithfully logged as truth. They are what make each row self-checking."""
    rows = observation_rows(
        "2026-08-29", "close", 1,
        [_lane("MOMENTUM-002", [_pos("MOMENTUM-002", "AEM.XNYS")])],
        currency="USD",
        mark_source="close",
        venue={"AEM.XNYS": {"qty": 9.0, "avg_px": 99.5, "unrealized": 94.5}},
    )
    assert rows[0]["qty_venue"] == 9.0
    assert rows[0]["avg_px_venue"] == 99.5
    # And the engine's own reading is still there beside it — the pair IS the detector.
    assert rows[0]["unrealized_engine"] == 90.0
    assert rows[0]["unrealized_venue"] == 94.5


def test_an_UNPRICED_position_records_no_mark_and_no_mark_source():
    """A mark_source on a row with no mark would describe a price that does not exist."""
    rows = observation_rows(
        "2026-08-29", "close", 1,
        [_lane("QC345-003", [_pos("QC345-003", "AMAT.XNAS", mark=None, engine_pnl=None)], unpriced=1)],
        currency="USD",
        mark_source="close",
    )
    assert rows[0]["mark_px"] is None
    assert rows[0]["mark_source"] is None
    assert rows[0]["mark_ts"] is None


def test_the_mark_SOURCE_distinguishes_a_close_from_a_live_quote_from_a_bar():
    """A close-to-close series must never silently contain a live quote. The read path filters on
    `capture_kind`; this column is how an AUDIT sees which kind of price it actually got."""
    lanes = [_lane("MOMENTUM-002", [_pos("MOMENTUM-002", "AEM.XNYS")])]
    assert observation_rows("d", "close", 1, lanes, currency="USD", mark_source="close")[0]["mark_source"] == "close"
    assert observation_rows("d", "intraday-11:00", 1, lanes, currency="USD", mark_source="live")[0]["mark_source"] == "live"
    assert observation_rows(
        "d", "reconstructed-eod", 1, lanes, currency="USD", mark_source="reconstructed-bar",
        provenance="reconstruction", attribution_coverage=1.0,
    )[0]["mark_source"] == "reconstructed-bar"


def test_a_RECONSTRUCTION_must_MEASURE_its_attribution_rather_than_be_given_a_constant():
    """THIS TEST USED TO PIN THE DEFECT AS DELIBERATE, which is the kumo-trading-strategies falsy-`or`
    precedent: a test asserting a fallback it should have been refusing.

    The mapping hardcoded `0.0` for every reconstructed row and had no parameter to carry a
    measurement — so a post-2026-08-17 backfill row, whose coverage is measured at 130/130, would
    have recorded "none of this qty is attributed" while carrying a `strategy_id` claiming it was.
    A constant standing in for a measurement is the QC27-allocated-equity shape exactly.

    It now REFUSES rather than inventing one."""
    # A lane that carries NO measurement of its own — which is what a caller building lanes by hand,
    # or a future observer that forgets to measure, would produce.
    import dataclasses
    lanes = [dataclasses.replace(
        _lane("MOMENTUM-002", [_pos("MOMENTUM-002", "AEM.XNYS")]), attribution_coverage=None)]

    with pytest.raises(ValueError, match="no measured attribution_coverage"):
        observation_rows(
            "d", "reconstructed-eod", 1, lanes, currency="USD",
            mark_source="reconstructed-bar", provenance="reconstruction",
        )

    got = observation_rows(
        "d", "reconstructed-eod", 1, lanes, currency="USD", mark_source="reconstructed-bar",
        provenance="reconstruction", attribution_coverage=0.62,
    )
    assert got[0]["attribution_coverage"] == 0.62, "the MEASUREMENT must survive to the row"


def test_EACH_LANE_records_its_OWN_coverage_in_one_batch():
    """The severed wire review found: `LaneObservation.attribution_coverage` was computed by both
    observers and read by NOTHING — one batch-wide scalar was stamped across every row. A
    reconstruction mixing attributed lanes with UNCLAIMED therefore could not be stored truthfully in
    a single call: one number would have lied about half the rows."""
    lanes = [
        _lane("MOMENTUM-002", [_pos("MOMENTUM-002", "AEM.XNYS")]),
        dataclasses_replace_coverage(_lane("UNCLAIMED", [_pos("UNCLAIMED", "WPM.XNYS")]), 0.0),
    ]
    rows = observation_rows(
        "d", "reconstructed-eod", 1, lanes, currency="USD", mark_source="reconstructed-bar",
        provenance="reconstruction",
    )
    got = {r["strategy_id"]: r["attribution_coverage"] for r in rows}
    assert got == {"MOMENTUM-002": 1.0, "UNCLAIMED": 0.0}


def test_an_ENGINE_row_is_fully_attributed_as_a_FACT_not_as_a_default():
    """Nautilus tags every Position with the strategy that opened it, so an engine-side observation
    is complete by construction. That is why 1.0 is allowed here and refused above — the difference
    is whether the number was established or assumed."""
    lanes = [_lane("MOMENTUM-002", [_pos("MOMENTUM-002", "AEM.XNYS")])]
    assert observation_rows(
        "d", "close", 1, lanes, currency="USD", mark_source="close"
    )[0]["attribution_coverage"] == 1.0


def test_a_mark_source_that_CONTRADICTS_the_row_is_REFUSED():
    """`mark_source` used to be computed from `capture_kind` + `provenance` — a pure function of two
    sibling columns, so it could never disagree with them and carried no information. Worse, it
    stamped "close" on a `cache.price(LAST)` reading taken twenty minutes after the bell, so a
    mis-scheduled capture would have labelled a mid-session live quote "close" — exactly what this
    column exists to expose. The caller supplies it; incoherent combinations are refused."""
    lanes = [_lane("MOMENTUM-002", [_pos("MOMENTUM-002", "AEM.XNYS")])]
    with pytest.raises(ValueError, match="unknown mark_source"):
        observation_rows("d", "close", 1, lanes, currency="USD", mark_source="guess")
    with pytest.raises(ValueError, match="provenance='reconstruction'"):
        observation_rows("d", "reconstructed-eod", 1, lanes, currency="USD", mark_source="close")
    with pytest.raises(ValueError, match="historical bar"):
        observation_rows(
            "d", "close", 1, lanes, currency="USD", mark_source="close",
            provenance="reconstruction", attribution_coverage=1.0,
        )


def test_the_session_fill_qty_defaults_to_zero_and_is_carried_when_known():
    """`qty_t == qty_{t-1} + session_fill_qty_t + transfers_t` is the split detector. A default of 0
    is honest for a lane that traded nothing; the caller supplies the real figure when it has it."""
    lanes = [_lane("MOMENTUM-002", [_pos("MOMENTUM-002", "AEM.XNYS")])]
    assert observation_rows("d", "close", 1, lanes, currency="USD", mark_source="close")[0]["session_fill_qty"] == 0.0
    got = observation_rows(
        "d", "close", 1, lanes, currency="USD", mark_source="close",
        session_fills={("MOMENTUM-002", "AEM.XNYS"): 4.0},
    )
    assert got[0]["session_fill_qty"] == 4.0


# ==================================================================================================
# THE MANIFEST — a flat lane is not a missing lane
# ==================================================================================================
def test_a_lane_that_held_NOTHING_still_gets_a_row_saying_so():
    """The whole reason the manifest exists. Without it, "MANUAL-001 held nothing on Tuesday" and
    "nobody asked about MANUAL-001 on Tuesday" are the same absence."""
    mans = manifest_rows("2026-08-29", "close", 1, [], known_lanes=["MANUAL-001"])
    assert len(mans) == 1
    assert mans[0]["status"] == "observed"
    assert mans[0]["instrument_count"] == 0


def test_a_lane_with_an_unpriced_leg_says_its_total_is_UNKNOWN_not_partial():
    """The detail line is what the next reader sees. "Partial" would be a lie: the lane's total is
    not a smaller number, it is an unknown one."""
    lanes = [_lane("QC345-003", [_pos("QC345-003", "AMAT.XNAS", mark=None, engine_pnl=None)], unpriced=1)]
    detail = manifest_rows("d", "close", 1, lanes)[0]["detail"]
    assert "UNKNOWN" in detail and "partial" in detail


def test_a_FAILED_capture_still_writes_a_row():
    """A failed capture with no row is indistinguishable from a day nobody asked about. This table
    exists partly so gaps are VISIBLE and backfillable rather than inferred later from silence."""
    row = failed_manifest_row("2026-08-29", "close", 1, "MOMENTUM-002", "cache unreadable: boom")
    assert row["status"] == "failed"
    assert "cache unreadable" in row["detail"]
    assert row["instrument_count"] == 0


def test_a_very_long_failure_reason_is_TRUNCATED_rather_than_rejected_by_the_column():
    """`detail` is String(256). An exception repr can be far longer, and a capture whose FAILURE row
    fails to insert is the worst of both worlds — no observation and no record that there was none."""
    row = failed_manifest_row("d", "close", 1, "L", "x" * 5000)
    assert len(row["detail"]) == 256


# ==================================================================================================
# THE ROUND TRIP — the only part that genuinely needs a database
# ==================================================================================================
@pytest.mark.needs_services
def test_a_second_BASE_row_is_SKIPPED_and_never_overwrites_the_first():
    """Re-running a capture must be idempotent AND must not supersede what is already recorded. A
    corrected derivation writes under a new `method_version`; updating in place would destroy the
    audit trail this table exists to provide.

    Run explicitly against a throwaway database:
        KUMO_DATABASE_URL=postgresql+asyncpg://... pytest -m needs_services
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.eod_observation_store import EodObservationStore

    engine = create_async_engine(os.environ["KUMO_DATABASE_URL"])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    store = EodObservationStore(session_factory_=sf)
    lanes = [_lane("MOMENTUM-002", [_pos("MOMENTUM-002", "AEM.XNYS")])]

    async def go():
        async with sf() as s:
            await s.execute(text("DELETE FROM eod_position_observation WHERE session_date = '1999-01-01'"))
            await s.execute(text("DELETE FROM eod_observation_manifest WHERE session_date = '1999-01-01'"))
            await s.commit()

        first = await store.write(
            observation_rows("1999-01-01", "close", 1, lanes, currency="USD", mark_source="close"),
            manifest_rows("1999-01-01", "close", 1, lanes),
        )
        # A DIFFERENT snapshot_ts and a different qty: if the store updated in place rather than
        # skipping, the stored row would change and the assertion below would catch it.
        second = await store.write(
            observation_rows("1999-01-01", "close", 2, [_lane("MOMENTUM-002", [_pos("MOMENTUM-002", "AEM.XNYS", qty=999.0)])], currency="USD", mark_source="close"),
            manifest_rows("1999-01-01", "close", 2, lanes),
        )
        async with sf() as s:
            rows = (await s.execute(text(
                "SELECT qty, snapshot_ts FROM eod_position_observation WHERE session_date = '1999-01-01'"
            ))).all()
        await engine.dispose()
        return first, second, rows

    first, second, rows = asyncio.run(go())
    assert first.written == 1 and first.skipped == 0
    # THE MANIFEST IS COUNTED SEPARATELY. It has no conflict clause — it is an attempt log — so
    # folding it into `written` would let a capture that stored NO observations still report a write.
    assert first.manifests_written == 1 and first.lanes_attempted == 1
    assert second.written == 0 and second.skipped == 1, "a re-run must SKIP, not write and not raise"
    # ...and the re-run's manifest row DOES land: a second attempt is a second fact, and the day's
    # status is the latest attempt. This is what makes a failed capture repairable.
    assert second.manifests_written == 1
    assert len(rows) == 1, "the base row must not be duplicated"
    assert rows[0][0] == 9.0 and rows[0][1] == 1, "the FIRST observation must survive untouched"


# ==================================================================================================
# THE CONFLICT PREDICATE MUST BE CONSTANT — the defect a one-row round-trip cannot see
# ==================================================================================================
def test_the_ON_CONFLICT_predicate_contains_NO_BIND_PARAMETERS():
    """MEASURED ON AN ACTUAL POSTGRES, then pinned here so it can never come back offline.

    `index_where=Model.capture_kind.in_(BASE_KINDS)` compiles to `WHERE capture_kind IN ($20, $21)`.
    Postgres must PROVE the ON CONFLICT predicate implies the partial index's predicate, and it can
    only do that when it knows the values. It plans a prepared statement with the values in hand for
    the first five executions and then considers a GENERIC plan without them — so the sixth row
    raises `InvalidColumnReferenceError: there is no unique or exclusion constraint matching the ON
    CONFLICT specification`, and the first five do not.

    Four readings that disagree exactly as that predicts, taken against postgres:16:

        50 rows / plan_cache_mode=force_custom_plan    OK
         1 row  / plan_cache_mode=force_generic_plan   FAILED
         5 rows / plan_cache_mode=auto                 OK
        20 rows / plan_cache_mode=auto                 FAILED

    THE EXISTING INTEGRATION TEST WRITES ONE ROW AND PASSES FOREVER. Paper's capture writes one row
    per open position — fourteen — so it would have died on the sixth every single evening, and the
    green suite would have said the round trip was proven. That is why this assertion is about the
    SQL and not about a successful write: a test that has to guess how many rows is enough is testing
    the fuse length, not the fuse.
    """
    from sqlalchemy.dialects import postgresql

    from api.eod_observation_store import base_insert

    # THE STORE'S OWN STATEMENT. This test used to build its own from `BASE_KIND_PREDICATE`, and
    # review proved that vacuous by reverting the store to `capture_kind.in_(BASE_KINDS)` — the exact
    # #737 form — and watching all 30 offline tests stay green. It was asserting that the CONSTANT
    # compiles clean, never that the store uses it. Compile what production sends, or the detector is
    # aimed one level away from the defect.
    stmt = base_insert({
        "session_date": "d", "strategy_id": "L", "instrument_id": "S", "capture_kind": "close",
        "snapshot_ts": 1, "qty": 1.0, "currency": "USD", "method_version": METHOD_VERSION,
    })
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql, "the store did not attach a conflict clause to a BASE row at all"

    # THE PREDICATE MUST BE THERE AT ALL, asserted before its shape. A mutant that deleted the
    # `index_where` outright survived the first version of this test: the conflict target then names
    # four columns that only a PARTIAL index covers, so Postgres finds no matching constraint and
    # raises — the same error as #737, from the opposite cause, and offline nothing noticed.
    conflict = sql.split("ON CONFLICT", 1)[1]
    assert "WHERE" in conflict, (
        "the conflict target has no predicate — it can only match the partial index WITH one"
    )
    where = conflict.split("WHERE", 1)[1]
    assert "capture_kind" in where and "close" in where, f"not the predicate I think it is: {where!r}"
    assert "%(" not in where and "$" not in where, (
        f"the ON CONFLICT predicate carries bind parameters: {where!r} — Postgres cannot match it to "
        f"a partial index once it plans generically, which it does from the sixth execution"
    )


def test_the_predicate_and_the_KIND_TUPLE_are_ONE_declaration_not_two():
    """The rule was written THREE times — the model's index, the migration's string, and the store's
    tuple — and the drift test only compared two of them. The third rendered differently and that was
    the defect. The predicate is now DERIVED from the tuple, so there is nothing left to drift.

    Mutating either side must move both."""
    from api.db.models import BASE_CAPTURE_KINDS, BASE_KIND_PREDICATE

    assert tuple(BASE_KINDS) == tuple(BASE_CAPTURE_KINDS), "the store must not keep its own tuple"
    for kind in BASE_CAPTURE_KINDS:
        assert f"'{kind}'" in BASE_KIND_PREDICATE, f"{kind} is in the tuple and not in the predicate"
    # And nothing EXTRA: a predicate wider than the tuple silently guards rows the store never
    # protects, which is the direction that looks harmless and is not.
    assert BASE_KIND_PREDICATE.count("'") == 2 * len(BASE_CAPTURE_KINDS)


@pytest.mark.needs_services
def test_a_capture_of_MORE_THAN_FIVE_positions_survives_the_generic_plan_switch():
    """THE SEAM, not the unit. The offline test above pins the SQL; this proves the actual write path
    against a real server past the point where Postgres changes how it plans.

    Six is the boundary; fourteen is what paper actually holds. It writes fourteen.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.eod_observation_store import EodObservationStore

    engine = create_async_engine(os.environ["KUMO_DATABASE_URL"])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    lane = _lane("MOMENTUM-002", [_pos("MOMENTUM-002", f"S{i}.XNYS") for i in range(14)])

    async def go():
        async with sf() as s:
            await s.execute(text("DELETE FROM eod_position_observation WHERE session_date = '1999-01-02'"))
            await s.commit()
        result = await store_write(sf, lane)
        await engine.dispose()
        return result

    async def store_write(sf_, lane_):
        return await EodObservationStore(session_factory_=sf_).write(
            observation_rows("1999-01-02", "close", 1, [lane_], currency="USD", mark_source="close"),
            manifest_rows("1999-01-02", "close", 1, [lane_]),
        )

    result = asyncio.run(go())
    assert result.written == 14, (
        f"only {result.written} of 14 rows landed — the sixth execution is where the ON CONFLICT "
        f"predicate stops matching the partial index"
    )


def test_a_NON_BASE_row_gets_no_conflict_clause_at_all():
    """The other branch of the one construction site, so the helper cannot quietly start guarding —
    or stop guarding — the wrong kinds. A non-base row is not a window base and nothing subtracts
    from it, so a second one is a second observation rather than a conflict."""
    from api.eod_observation_store import base_insert

    sql = str(base_insert({
        "session_date": "d", "strategy_id": "L", "instrument_id": "S", "capture_kind": "intraday",
        "snapshot_ts": 1, "qty": 1.0, "currency": "USD", "method_version": METHOD_VERSION,
    }).compile(dialect=__import__("sqlalchemy.dialects", fromlist=["postgresql"]).postgresql.dialect()))
    assert "ON CONFLICT" not in sql


def test_NO_OTHER_upsert_in_the_repo_targets_a_partial_index_with_BIND_PARAMETERS():
    """#737 AS A REPO RULE, not a fix to the one site I tripped over.

    A parameterised ON CONFLICT predicate works for five executions and then raises, so it passes
    every offline test and every integration test that writes fewer than six rows. That is a defect
    class that arrives green, and enumerating its sites by hand is the thing this repo has repeatedly
    got wrong — so this scans instead.

    Surveyed at the time of writing: `command_ledger` (do_nothing on a FULL unique index),
    `cycle_store` (do_update on the primary key; its `where=` is the update guard, which may carry
    binds legitimately), `execquality/store` (do_update on a full constraint). None can hit #737.
    LATENT: alembic 0015 creates two unique PARTIAL indexes on `exec_action_log` (`kind = 'decision'`)
    and nothing upserts against them today — the first person who does will meet this, which is
    exactly who this test is written for.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    scanned = 0
    for path in root.rglob("*.py"):
        if "test_" in path.name or ".venv" in str(path) or "alembic" in str(path):
            continue
        body = path.read_text()
        for m in re.finditer(r"index_where\s*=\s*([^\n]+)", body):
            scanned += 1
            expr = m.group(1)
            # `text(...)` renders literally; anything else is a SQLAlchemy expression whose values
            # become bind parameters. The construct, not the value, is what decides.
            if "text(" not in expr:
                offenders.append(f"{path.relative_to(root)}: {expr.strip()[:80]}")

    # FIXTURE PROPERTY FIRST: a scan that matched nothing would pass forever and say nothing. The
    # store's own site must be found, or the pattern has drifted and this test is asleep.
    assert scanned >= 1, "the scan found no index_where= at all — it is no longer looking at anything"
    assert not offenders, (
        "these ON CONFLICT predicates carry bind parameters and will raise from the sixth row "
        f"(#737): {offenders}"
    )


# ==================================================================================================
# READING BACK — what the window delta subtracts from
# ==================================================================================================
def test_the_read_asks_for_ONE_DAY_and_only_BASE_kinds():
    """PURE, so it can be checked without a database — the same split as `observation_rows`, and for
    the same reason: a query that could only be exercised under `needs_services` would be effectively
    unexercised, since that job runs nowhere unattended.

    ONLY BASE KINDS. A window base is what a period return SUBTRACTS FROM, and only a close (or a
    reconstruction standing in for a missing one) is that. An intraday probe is a reading at an
    arbitrary moment; subtracting from it reports the move since lunchtime as the move since Monday.
    The database's uniqueness guarantee is partial on exactly these kinds too, so a non-base row is
    not even guaranteed to be alone.
    """
    from api.eod_observation_store import base_rows_query

    sql = str(base_rows_query("2026-08-24").compile(compile_kwargs={"literal_binds": True}))
    assert "eod_position_observation" in sql
    assert "2026-08-24" in sql
    # FIXTURE PROPERTY FIRST: the compiled text must actually contain a WHERE, or the assertions
    # below pass against a query that filters nothing.
    assert "WHERE" in sql, f"no predicate compiled at all: {sql[:200]}"
    for kind in BASE_KINDS:
        assert f"'{kind}'" in sql, f"{kind} is a base kind and the query does not ask for it"
    assert "'intraday'" not in sql


@pytest.mark.needs_services
def test_a_written_observation_can_be_READ_BACK_and_only_the_base_row_comes_out():
    """THE SEAM. The mapping is checked offline above; this proves a row written by this store is
    found by this store's own read — the round trip, against a real server, with a non-base row
    present to prove the filter is doing something."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.eod_observation_store import EodObservationStore

    engine = create_async_engine(os.environ["KUMO_DATABASE_URL"])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    store = EodObservationStore(session_factory_=sf)
    lanes = [_lane("MOMENTUM-002", [_pos("MOMENTUM-002", "AEM.XNYS")])]

    async def go():
        async with sf() as s:
            await s.execute(text("DELETE FROM eod_position_observation WHERE session_date='1999-02-02'"))
            await s.commit()
        await store.write(
            observation_rows("1999-02-02", "close", 1, lanes, currency="USD", mark_source="close"),
            [],
        )
        # A non-base row for the SAME key, which the read must not return.
        await store.write(
            observation_rows("1999-02-02", "intraday", 2, lanes, currency="USD", mark_source="live"),
            [],
        )
        rows = await store.base_rows("1999-02-02")
        await engine.dispose()
        return rows

    rows = asyncio.run(go())
    assert len(rows) == 1, f"only the base row may come back; got {[r['capture_kind'] for r in rows]}"
    assert rows[0]["capture_kind"] == "close"
    assert rows[0]["strategy_id"] == "MOMENTUM-002"
    assert rows[0]["unrealized_engine"] is not None


def test_every_column_the_READ_projects_is_a_REAL_column_on_the_model():
    """M12, AND THE FIX FOR IT HAD TO BE A TEST, NOT A CODE CHANGE.

    Review renamed `mark_px` to `mark_pxx` inside `base_rows`'s column tuple and ALL 2992 TESTS
    PASSED. `getattr(r, c, None)` turned the typo into `mark_px: None` on every row, so every lane
    read unknown, every period nulled, and the feature went permanently DARK with an empty
    `unreadable` list and a green suite.

    I removed the default so drift RAISES — and the mutant still survived, because `base_rows`
    executes only under `needs_services`, which the default run deselects and which this repo has
    established runs nowhere unattended. A guard on a path no offline test takes is not a guard.

    So the projection is bound to the model HERE, offline. This is the same shape as the phantom
    `unrealized_derived` column earlier in this chain: a name that does not exist, silently None,
    and nothing to notice it.
    """
    import inspect

    from api.db.models import EodPositionObservation
    from api.eod_observation_store import EodObservationStore

    src = inspect.getsource(EodObservationStore.base_rows)
    names = set(re.findall(r'"([a-z_]+)"', src.split("cols = (")[1].split(")")[0]))
    real = {c.name for c in EodPositionObservation.__table__.columns}

    # FIXTURE PROPERTY FIRST, BOTH SIDES: a scrape that found nothing would agree with anything.
    assert len(names) >= 8, f"the projection scrape found only {names} — it is no longer reading it"
    assert "mark_px" in names, "the scrape no longer sees a column the read certainly projects"

    # BOTH DIRECTIONS. The first version asserted only `names ⊆ real`, so a TYPO failed but a
    # DELETION did not: review removed `"qty"` and the full suite stayed green — `r.get("qty")`
    # returns None, every lane reads unknown, and the feature goes permanently dark with an empty
    # `unreadable`. Subset catches the wrong name; superset catches the missing one.
    required = {"session_date", "strategy_id", "instrument_id", "capture_kind", "method_version",
                "qty", "avg_px_engine", "mark_px"}
    dropped = sorted(required - names)
    assert not dropped, (
        f"the read no longer projects {dropped}, which `eod_read` needs. Every row would carry them "
        f"as None and every lane would read unknown — dark, with a green suite."
    )

    unknown = sorted(names - real)
    assert not unknown, (
        f"the read projects columns that do not exist on the model: {unknown}. Every row would carry "
        f"them as None, every lane would read unknown, and the whole feature would go dark with a "
        f"green suite — which is exactly how it was found."
    )


def test_the_captured_day_query_resolves_to_the_NEWEST_day_and_is_BOUNDED():
    """N2, and it is the #737 lesson arriving a third time in this same file.

    `base_insert` and `base_rows_query` are extracted so a test can compile PRODUCTION's statement —
    and I then wrote a third query inline and bypassed the pattern. Review flipped its ORDER BY from
    DESC to ASC and the full 2999-test suite stayed GREEN, because the offline double reimplements
    the walk-back itself and nothing bound production's SQL to it. ASC resolves to the OLDEST day in
    the window, serving a stale base under a confident resolved label.
    """
    from sqlalchemy.dialects import postgresql

    from api.eod_observation_store import BASE_LOOKBACK_DAYS, captured_day_query

    sql = str(captured_day_query("2026-08-31", BASE_LOOKBACK_DAYS)
              .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    # FIXTURE PROPERTY FIRST: this really is the manifest query, or the assertions below are about
    # some other statement entirely.
    assert "eod_observation_manifest" in sql, f"not the manifest query: {sql[:160]}"

    assert "DESC" in sql.upper(), "ASC would resolve to the OLDEST day in the window"
    assert "2026-08-31" in sql and "2026-08-27" in sql, "the lookback must be bounded to 4 days"
    # THE MANIFEST, NOT THE ROWS. A day captured with every lane flat writes zero base rows and is
    # indistinguishable from a day never captured — resolving on rows fabricates a stale base.
    assert "status" in sql and "failed" in sql, "a failed capture is not a base"


def test_the_coverage_query_reads_the_MANIFEST_since_a_bound_and_skips_FAILED_captures():
    """#699 a: `lane_net_terms` reads which sessions have a close and when each lane was first seen.
    The same #737 pattern — compile PRODUCTION's statement. A failed capture is not coverage (it
    would render a #1040 gap as captured); the bound keeps it one indexed range scan."""
    from sqlalchemy.dialects import postgresql

    from api.eod_observation_store import coverage_query

    sql = str(coverage_query("2026-06-06")
              .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "eod_observation_manifest" in sql, f"not the manifest query: {sql[:160]}"
    assert "strategy_id" in sql and "session_date" in sql
    assert ">= '2026-06-06'" in sql
    assert "status" in sql and "failed" in sql, "a failed capture is not coverage"
    assert "close" in sql, "only base kinds are coverage — an intraday capture is not a close"


def test_coverage_folds_the_manifest_rows_into_first_observed_and_captured():
    """The store's own fold, driven with the row shape the query yields."""
    import asyncio

    from api.eod_observation_store import EodObservationStore

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt):
            return _Result([("2026-09-04", "BCTROT-004"), ("2026-09-11", "MOMENTUM-002"),
                            ("2026-09-08", "BCTROT-004"), ("2026-09-04", "TECHIVOL-005")])

    store = EodObservationStore(session_factory_=lambda: _Session())
    cov = asyncio.run(store.coverage("2026-09-01"))
    assert cov == {"first_observed": {"BCTROT-004": "2026-09-04", "MOMENTUM-002": "2026-09-11",
                                      "TECHIVOL-005": "2026-09-04"},
                   "captured": {"2026-09-04", "2026-09-08", "2026-09-11"}}
