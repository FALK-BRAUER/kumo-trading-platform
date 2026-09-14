"""Persisting an observation of the book — the writer half of #734.

WHAT IT WRITES AND WHAT IT REFUSES TO. Rows are OBSERVATIONS: append-only, dated, never read by
trading logic. A base row (`close` / `reconstructed-eod`) is unique per lane-instrument-day and the
database enforces it (`uq_eod_observation_base`, migration 0017); this store never updates one in
place. A re-run therefore writes NOTHING rather than silently superseding what is already recorded —
and it SAYS how many rows it skipped, because a write that quietly did nothing and a write that
quietly succeeded look identical afterwards.

A CORRECTED RE-DERIVATION IS A NEW `method_version`, NEVER AN UPDATE. That is what makes the table
re-derivable: the old rows stay, the new rows stand beside them, and the two can be diffed. Updating
in place would destroy exactly the audit trail the shape exists to provide.

THE MANIFEST IS NOT OPTIONAL. Every capture writes one row per lane saying `observed` (with a count,
which may be 0), `failed` (with the reason), or `absent`.

THIS STORE ALWAYS INSERTS WHAT IT IS GIVEN — the manifest has no conflict clause, deliberately, and
that is load-bearing: its first version had a unique index which refused the repair when a capture
failed at 16:20 and succeeded at 16:30, freezing the day as `failed` while correct observation rows
sat beside it. Both attempts were true facts.

WHAT COUNTS AS AN ATTEMPT WORTH RECORDING IS THE CALLER'S CALL, and the live capture answers it as
TRANSITIONS: it asks every five minutes, so an unresolved condition would write about forty identical
`failed` rows in a day, and forty rows read as forty failures rather than one. It writes a row when
the NORMALIZED reason changes, and logs every attempt unconditionally — the table records
transitions, the log records attempts. See `eod_hook._record_failure`, which also says what that
costs: a single row cannot bound an UNRESOLVED failure's duration, which is why the per-tick log line
is not redundant.

(An earlier version of this comment argued the collapse should not happen at all, on the grounds that
the attempt count would become unrecoverable. That was wrong in one respect and it mattered: the
count is not lost, it moves to the log. Two questions, two artifacts.) Without it a lane that held nothing and a
capture that never ran are the same absence — this repo's most-repeated defect class, and the reason
`observe_lanes` RAISES on an unreadable cache instead of returning an empty list.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.db.engine import session_factory
from api.db.models import (
    BASE_CAPTURE_KINDS,
    BASE_KIND_PREDICATE,
    EodObservationManifest,
    EodPositionObservation,
)

#: Bump when the DERIVATION changes — a different basis rule, a different mark convention, a fixed
#: attribution bug. Rows carry it so history computed under an old rule stays identifiable and a
#: re-derivation can stand beside it rather than overwrite it.
METHOD_VERSION = "v1"

#: RE-EXPORTED, not redeclared. This was a THIRD copy of the rule — the model had it as a literal
#: string, migration 0017 has it as `_BASE_KINDS`, and this had it as a tuple. The drift test compared
#: two of the three and passed, while the third rendered as bind parameters and broke every capture
#: past five rows. Migration 0017 still writes it out (a migration must not import application code
#: that will change under it), and the drift test pins that against this.
BASE_KINDS = BASE_CAPTURE_KINDS

#: Where a mark came from. Declared so a typo is refused rather than stored — a `mark_source` nobody
#: recognises is worse than none, because a reader assumes it means something.
_MARK_SOURCES = frozenset({"live", "close", "reconstructed-bar"})


@dataclass(frozen=True)
class WriteResult:
    """What actually happened, in three states rather than two.

    `skipped` is not a failure and not a success — it means a base row already existed for that key,
    which on a re-run is correct and expected. Reporting it separately is what stops "wrote 0 rows
    because everything was already there" being indistinguishable from "wrote 0 rows because the book
    was empty" or "wrote 0 rows because something broke".
    """

    written: int
    skipped: int
    #: Manifest rows actually inserted. Separate from `written`, because the manifest is an ATTEMPT
    #: log with no conflict clause: it always inserts, and reporting it inside the observation count
    #: would let a capture that stored NO observations still read as having written something.
    manifests_written: int
    #: How many lanes this capture was ASKED about — an input, not an outcome. It used to be
    #: `len(manifests)` under the name `lanes`, which is a count of attempts dressed as a result.
    lanes_attempted: int


def observation_rows(
    session_date: str,
    capture_kind: str,
    snapshot_ts: int,
    lanes,
    *,
    #: NO DEFAULT, deliberately (#734 review). It used to default to "USD" beside a column comment
    #: saying staging is SGD — the missing-argument-invisible pattern, recording a plausible lie on
    #: the one instance where it is wrong. The caller knows the account's currency; it must say it.
    currency: str,
    #: WHERE THE PRICE ACTUALLY CAME FROM — a fact the caller holds, not a conclusion derived here.
    #: It used to be computed from `capture_kind` + `provenance`, which made it a pure function of two
    #: sibling columns in the same row: it could never disagree with them, so it carried no
    #: information, and it stamped "close" on a `cache.price(LAST)` reading taken twenty minutes after
    #: the bell. A mis-scheduled capture would then have labelled a mid-session live quote "close" —
    #: which is precisely what this column exists to expose.
    mark_source: str,
    #: A BATCH-WIDE OVERRIDE, used only when the lanes do not carry their own measurement. Each
    #: `LaneObservation` now measures its own coverage and THAT is what lands on the row — the
    #: earlier version stamped one scalar across every row in the batch, so a reconstruction mixing
    #: attributed lanes (1.0) with UNCLAIMED (0.0) could not be stored truthfully in one call: one
    #: number would have lied about half the rows. Computed-and-discarded, in the commit that cited
    #: the rule against it.
    attribution_coverage: float | None = None,
    provenance: str = "engine",
    method_version: str = METHOD_VERSION,
    venue: dict[str, dict] | None = None,
    session_fills: dict[tuple[str, str], float] | None = None,
) -> list[dict]:
    """Map lane observations to row values. PURE — no database, no clock, no cache.

    Kept separate from the write so the mapping is testable offline. The integration test needs a
    live Postgres and is `needs_services`, which the default suite deselects; a mapping that could
    only be checked there would be effectively unchecked.

    `venue` carries the broker's own reading per instrument (`qty`, `avg_px`, `unrealized`) where one
    is available. It is OPTIONAL and its absence is recorded as NULL, which is not the same as the
    broker agreeing — three states, and #370's rule.
    """
    venue = venue or {}
    session_fills = session_fills or {}

    if mark_source not in _MARK_SOURCES:
        raise ValueError(f"unknown mark_source {mark_source!r}; expected one of {sorted(_MARK_SOURCES)}")
    # REFUSE THE COMBINATIONS THAT CANNOT BE TRUE, rather than mapping them to something plausible.
    # `reconstructed-eod` rows are built from historical bars; an `engine` provenance there is a
    # caller mistake, and silently stamping it "close" would put a fabricated label on a real row.
    if capture_kind == "reconstructed-eod" and provenance != "reconstruction":
        raise ValueError("a reconstructed-eod row must carry provenance='reconstruction'")
    if provenance == "reconstruction" and mark_source != "reconstructed-bar":
        raise ValueError("a reconstruction's mark comes from a historical bar; say so")

    # EVERY LANE MUST CARRY ITS OWN MEASUREMENT, or the batch override must supply one. A lane whose
    # coverage is unknown is refused rather than given a plausible number.
    for lane in lanes:
        if getattr(lane, "attribution_coverage", None) is None and attribution_coverage is None:
            raise ValueError(
                f"lane {lane.strategy_id!r} carries no measured attribution_coverage and no batch "
                f"override was given — the fill-to-order join is 100% only from 2026-08-17 and ~1% "
                f"before it, so a constant standing in for that measurement is how a lane claims an "
                f"attribution it never established"
            )
    rows: list[dict] = []
    for lane in lanes:
        for pos in lane.positions:
            v = venue.get(pos.instrument_id) or {}
            rows.append(
                {
                    "session_date": session_date,
                    "strategy_id": pos.strategy_id,
                    "instrument_id": pos.instrument_id,
                    "capture_kind": capture_kind,
                    "snapshot_ts": snapshot_ts,
                    "qty": pos.qty,
                    "avg_px_engine": pos.avg_px_engine,
                    "avg_px_venue": v.get("avg_px"),
                    "qty_venue": v.get("qty"),
                    "mark_px": pos.mark_px,
                    # The mark's PROVENANCE, not a judgement about it. A close-to-close series must
                    # never silently contain a live quote, and this is how an audit sees which it got.
                    # None when there is no mark: a source would describe a price that does not exist.
                    "mark_source": None if pos.mark_px is None else mark_source,
                    "mark_ts": snapshot_ts if pos.mark_px is not None else None,
                    "currency": currency,
                    "unrealized_engine": pos.unrealized_engine,
                    "unrealized_venue": v.get("unrealized"),
                    "session_fill_qty": session_fills.get((pos.strategy_id, pos.instrument_id), 0.0),
                    "provenance": provenance,
                    "method_version": method_version,
                    # THE LANE'S OWN measurement wins; the batch override is the fallback for a
                    # caller that has one figure for everything. UNCLAIMED rows therefore record 0.0
                    # in the same batch where an attributed lane records 1.0.
                    "attribution_coverage": (
                        lane.attribution_coverage
                        if getattr(lane, "attribution_coverage", None) is not None
                        else attribution_coverage
                    ),
                }
            )
    return rows


def manifest_rows(
    session_date: str,
    capture_kind: str,
    snapshot_ts: int,
    lanes,
    *,
    known_lanes=(),
    #: Positions the observer could NOT describe (`Observation.skipped`). A capture that silently
    #: dropped a held position while reporting "observed, N-1" with a clean detail line would be the
    #: absence-as-flatness defect one seam further out — which is exactly how the earlier fix for it
    #: was rebuilt: prose promising the caller would use this, and no parameter to pass it through.
    skipped=(),
    provenance: str = "engine",
    method_version: str = METHOD_VERSION,
) -> list[dict]:
    """One row per lane, INCLUDING the lanes that held nothing.

    `known_lanes` is every lane the capture was asked about. A lane in that list with no positions is
    `observed` with `instrument_count=0` — a fact. A lane absent from BOTH lists never gets a row,
    and a lane that could not be read is the caller's `failed`. Absence must never be readable as
    flatness.
    """
    seen = {lane.strategy_id: lane for lane in lanes}
    out: list[dict] = []
    for strategy_id in sorted(set(seen) | set(known_lanes)):
        lane = seen.get(strategy_id)
        out.append(
            {
                "session_date": session_date,
                "strategy_id": strategy_id,
                "capture_kind": capture_kind,
                "snapshot_ts": snapshot_ts,
                # A CAPTURE THAT COULD NOT DESCRIBE PART OF THE BOOK IS NOT A CLEAN `observed`.
                # `degraded` is its own state: something was held and is missing from these rows.
                "status": "degraded" if skipped else "observed",
                "instrument_count": len(lane.positions) if lane else 0,
                "detail": _detail(lane, skipped),
                "provenance": provenance,
                "method_version": method_version,
            }
        )
    return out


def _detail(lane, skipped) -> str | None:
    """What this lane can honestly say about its own completeness.

    The skipped list is not filtered per lane on purpose: a position with no readable quantity or no
    attributable lane has, by definition, no lane to file it under. Reporting it on every lane's row
    over-reports rather than losing it, and over-reporting a gap is the safe direction.
    """
    parts = []
    if lane is not None and lane.unpriced:
        parts.append(f"{lane.unpriced} position(s) had no mark — this lane's total is UNKNOWN, not partial")
    if skipped:
        parts.append(f"{len(skipped)} position(s) could not be described at all: " + "; ".join(skipped)[:150])
    return " · ".join(parts) or None


def failed_manifest_row(
    session_date: str,
    capture_kind: str,
    snapshot_ts: int,
    strategy_id: str,
    detail: str,
    *,
    provenance: str = "engine",
    method_version: str = METHOD_VERSION,
) -> dict:
    """A capture that could not read the book. THE ROW STILL GETS WRITTEN — that is the whole point.

    A failed capture with no row is indistinguishable from a day nobody asked about, and this table
    exists partly so that gaps are visible and backfillable rather than inferred later from silence.
    """
    return {
        "session_date": session_date,
        "strategy_id": strategy_id,
        "capture_kind": capture_kind,
        "snapshot_ts": snapshot_ts,
        "status": "failed",
        "instrument_count": 0,
        "detail": detail[:256],
        "provenance": provenance,
        "method_version": method_version,
    }


def base_insert(row: dict):
    """The INSERT the store actually sends, built here so a test can compile THE SAME statement.

    IT USED TO BE INLINE IN `write`, and the test that guards it compiled a statement of its own from
    `BASE_KIND_PREDICATE`. Review proved that vacuous: reverting `write` to
    `capture_kind.in_(BASE_KINDS)` — the exact form that caused #737 — left the whole offline suite
    green, because the test was checking that the CONSTANT compiles clean, never that the store uses
    it. The only thing that caught the regression was the 14-row `needs_services` test, which the
    default suite deselects and which this repo has already established never runs unattended.

    A detector must be aimed at the level the defect lives at. So the statement has one construction
    site and the test compiles that.

    THE CONFLICT TARGET MUST MATCH `uq_eod_observation_base` EXACTLY, including `method_version`.
    Postgres validates this: a mismatch raises `InvalidColumnReferenceError: there is no unique or
    exclusion constraint matching the ON CONFLICT specification` — loudly, which is how adding
    `method_version` to the index was caught rather than silently degrading into unguarded inserts.

    And `text(BASE_KIND_PREDICATE)`, NOT `capture_kind.in_(...)`: the `in_()` form compiles the kinds
    to BIND PARAMETERS, and Postgres can only prove a parameterised predicate matches the partial
    index while it still knows the values — which it stops doing when it switches to a generic plan
    on the sixth execution. Measured, not reasoned: `force_custom_plan` wrote 50 rows,
    `force_generic_plan` failed on row 1. See #737.
    """
    stmt = pg_insert(EodPositionObservation).values(**row)
    if row["capture_kind"] not in BASE_KINDS:
        # A non-base row (an intraday probe, a repair) is not guarded — it is not a window base and
        # nothing subtracts from it, so a duplicate is a duplicate observation and not a conflict.
        return stmt
    return stmt.on_conflict_do_nothing(
        index_elements=["session_date", "strategy_id", "instrument_id", "method_version"],
        index_where=text(BASE_KIND_PREDICATE),
    )


#: How far back a window base may be resolved. A weekend is two days and a long weekend three, so
#: four covers every non-holiday gap; beyond that the day genuinely was not captured and the honest
#: answer is UNKNOWN. Bounded deliberately — an unbounded search would silently reach past a real
#: capture outage and report a stale base as this window's start.
BASE_LOOKBACK_DAYS = 4


def captured_day_query(session_date: str, lookback_days: int = BASE_LOOKBACK_DAYS):
    """The newest day at or before `session_date` whose capture SUCCEEDED — from the MANIFEST.

    EXTRACTED SO A TEST COMPILES PRODUCTION'S OWN STATEMENT. `base_insert` and `base_rows_query`
    exist in this file for exactly that reason (#737: a test that compiled its own statement let the
    real defect back in with 30 tests green) — and I then wrote a third query inline and bypassed the
    pattern. Review flipped its `ORDER BY` from DESC to ASC and the full suite stayed green, because
    the offline double reimplements the walk-back itself and nothing bound production's SQL to it.
    DESC is load-bearing: ASC resolves to the OLDEST day in the window under a confident label.

    `status != 'failed'` rather than `== 'observed'`: `degraded` means the book was read and part of
    it could not be described — a real capture with a stated gap, which is still a better base than
    reaching further back. `failed` means nothing was read.
    """
    from datetime import date, timedelta

    from sqlalchemy import select

    earliest = (date.fromisoformat(session_date) - timedelta(days=lookback_days)).isoformat()
    return (
        select(EodObservationManifest.session_date)
        .where(EodObservationManifest.session_date <= session_date,
               EodObservationManifest.session_date >= earliest,
               EodObservationManifest.capture_kind.in_(BASE_KINDS),
               EodObservationManifest.status != "failed")
        .order_by(EodObservationManifest.session_date.desc())
        .limit(1)
    )


def coverage_query(since: str):
    """Every (session_date, strategy_id) the MANIFEST says was successfully captured since `since` —
    the two facts `lane_net_terms` needs: which sessions have a close at all, and when each lane
    was first observed. Same `status != 'failed'` reading as `captured_day_query`, for the same
    reason; extracted so a test compiles production's own statement."""
    from sqlalchemy import select

    return (
        select(EodObservationManifest.session_date, EodObservationManifest.strategy_id)
        .where(EodObservationManifest.session_date >= since,
               EodObservationManifest.capture_kind.in_(BASE_KINDS),
               EodObservationManifest.status != "failed")
    )


def base_rows_query(session_date: str):
    """The SELECT behind `base_rows`, built here so a test can compile the STATEMENT the store sends.

    Separate from the execution for the reason the whole file is: a query only exercisable under
    `needs_services` is effectively unexercised, because that job runs nowhere unattended. This is
    also the lesson of #737 — a test that compiled its OWN statement rather than the store's let the
    real defect back in with 30 tests green.

    ONLY BASE KINDS. A window base is what a period return subtracts FROM, and only a close, or a
    reconstruction standing in for a missing one, is that. `uq_eod_observation_base` is partial on
    exactly these kinds, so a non-base row carries no uniqueness guarantee either.
    """
    from sqlalchemy import select

    return select(EodPositionObservation).where(
        EodPositionObservation.session_date == session_date,
        EodPositionObservation.capture_kind.in_(BASE_KINDS),
    )




class EodObservationStore:
    """Async persistence for the observation log. `session_factory_` is injectable so tests can point
    at a throwaway database — the same shape as `CycleEnvelopeStore`."""

    def __init__(self, session_factory_=None) -> None:
        self._sf = session_factory_ or session_factory

    async def base_rows_on_or_before(self, session_date: str,
                                     lookback_days: int = BASE_LOOKBACK_DAYS) -> tuple[str | None, list[dict]]:
        """The newest SUCCESSFULLY CAPTURED day at or before `session_date`, and its base rows.

        WHY THE MANIFEST DECIDES THE DAY AND NOT THE ROWS. My first version picked the newest day
        that HAD base rows — and a day where capture SUCCEEDED with every lane FLAT writes ZERO rows,
        which is indistinguishable from a day never captured. It would then skip that day and serve
        an OLDER day's unrealized as this window's base: `now − 500` where the truth is `now − 0`.

        The version before THAT refused honestly (exact day or nothing). So the fix for the weekend
        gap traded a refusal for a FABRICATION, which is worse than what it replaced. The manifest is
        the only plane that separates "captured and flat" from "never captured" — that is why it
        exists, and both docstrings in this chain promised it would be consulted while nothing
        consulted it.

        A lane absent from a manifest-successful day therefore has a base of ZERO, not unknown.

        WHY AT-OR-BEFORE AT ALL. Measured over 28 days: 1M's base lands on a weekend on 8 of 20
        trading weekdays, 1D every Monday, 3M every Friday. Marks do not move over a weekend, so
        Friday's close IS the unrealized prevailing at a Saturday boundary. BOUNDED to four days so a
        genuine capture outage still reads UNKNOWN rather than borrowing a stale base.
        """
        async with self._sf() as session:
            found = (await session.execute(
                captured_day_query(session_date, lookback_days)
            )).scalars().first()
        if found is None:
            return None, []
        return str(found), await self.base_rows(str(found))

    async def coverage(self, since: str) -> dict:
        """`{"first_observed": {lane: date}, "captured": {date, ...}}` from the manifest since `since`.

        `first_observed` is bounded by `since` — a lane first seen BEFORE it reads as first seen at
        the oldest row inside the bound, which is at or before every base the caller can ask about,
        so the "born inside the window" test cannot be fooled by the bound. RAISES on a failed read:
        fabricating empty coverage would render every window as un-partial (a fallback that lies).
        """
        async with self._sf() as session:
            rows = (await session.execute(coverage_query(since))).all()
        first: dict[str, str] = {}
        captured: set[str] = set()
        for day, lane in rows:
            d, l = str(day), str(lane)
            captured.add(d)
            if l not in first or d < first[l]:
                first[l] = d
        return {"first_observed": first, "captured": captured}

    async def base_rows(self, session_date: str) -> list[dict]:
        """Every base observation for one day, as plain dicts.

        DICTS RATHER THAN ORM OBJECTS, deliberately: `eod_read` is pure and must stay testable
        without a database session, and handing it detached instances would make its behaviour
        depend on whether a session was still open.
        """
        cols = ("session_date", "strategy_id", "instrument_id", "capture_kind", "method_version",
                "qty", "avg_px_engine", "mark_px", "unrealized_engine", "unrealized_venue",
                "currency", "attribution_coverage")
        async with self._sf() as session:
            found = (await session.execute(base_rows_query(session_date))).scalars().all()
        # NO DEFAULT ON `getattr`. With one, a typo in `cols` becomes `mark_px: None` on every row,
        # every lane reads unknown, every period nulls, and the feature goes DARK PERMANENTLY with an
        # empty `unreadable` and a green suite. Review proved it: renaming `mark_px` to `mark_pxx`
        # here survived all 2992 tests. That is this repo's own "a fallback is a silent wrong
        # answer", and I had already been bitten once in this same chain by a column that did not
        # exist. Drift must raise.
        return [{c: getattr(r, c) for c in cols} for r in found]

    async def write(self, rows: list[dict], manifests: list[dict]) -> WriteResult:
        """Insert observations and manifests, skipping base rows that already exist.

        `on_conflict_do_nothing` against the partial index, NEVER `do_update`. A base row is a record
        of what was observed at a moment; overwriting it would destroy the audit trail and break the
        re-derivability this table exists for. A corrected derivation writes under a new
        `method_version` instead.

        The skip count is RETURNED rather than logged, because the caller is the only thing that
        knows whether a skip is expected (a re-run) or a surprise (two captures racing for one slot).
        """
        written = 0
        async with self._sf() as session:
            for row in rows:
                stmt = base_insert(row)
                result = await session.execute(stmt)
                written += int(result.rowcount or 0)
            # NO CONFLICT CLAUSE ON THE MANIFEST — it is an ATTEMPT log. A capture that failed at
            # 16:20 and succeeded at 16:30 produced TWO true facts, and the earlier unique index
            # refused the repair, freezing the day as `failed` while the observation rows beside it
            # were correct. The day's status is the LATEST attempt; the failure stays visible.
            manifests_written = 0
            for man in manifests:
                result = await session.execute(pg_insert(EodObservationManifest).values(**man))
                manifests_written += int(result.rowcount or 0)
            await session.commit()
        return WriteResult(
            written=written,
            skipped=len(rows) - written,
            manifests_written=manifests_written,
            lanes_attempted=len(manifests),
        )
