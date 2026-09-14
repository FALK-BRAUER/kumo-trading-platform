"""Walking sessions and writing history — and every state that is not a clean write (#734).

THE PRECONDITION IS THE SUBJECT. If the acceptance gate has not passed, this must write NOTHING;
a gate that the runner could reach the write path without is decoration. The tests below drive the
runner, not the pieces, because every defect this repo has paid for lately was in the WIRING.
"""

from __future__ import annotations

import asyncio

import pytest

from api.eod_backfill import AgreementReport, Disagreement, fingerprint
from api.eod_backfill_runner import (
    CAPTURE_KIND, MARK_SOURCE, PROVENANCE, backfill_sessions, session_deltas,
)
from api.eod_observation_store import WriteResult

#: A report CERTIFIED AGAINST `FILLS` — the fingerprint is not decoration, it is what makes the
#: report a statement about this ledger rather than a token the caller happens to hold. Building it
#: by hand with the right fingerprint is exactly what a real gate run produces.
def _certified(**kw):
    return AgreementReport(compared=2, engine_pairs=2,
                           activities_fingerprint=fingerprint(FILLS), **kw)


class _Store:
    """Records what it was asked to write. Returns a WriteResult shaped like the real store's —
    including the three-state skip count, because a runner that could not tell a re-run from an empty
    book would be the defect the store's own docstring exists to prevent."""

    def __init__(self, already_present: int = 0):
        self.calls: list[tuple[list, list]] = []
        self._already = already_present

    async def write(self, rows, manifests):
        self.calls.append((rows, manifests))
        written = max(0, len(rows) - self._already)
        return WriteResult(written=written, skipped=len(rows) - written,
                           manifests_written=len(manifests), lanes_attempted=len(manifests))


def _fill(sym, side, qty, price, t, ):
    return {"symbol": sym, "side": side, "qty": str(qty), "price": str(price), "transaction_time": t}


T1 = "2026-08-20T13:00:00Z"
T2 = "2026-08-24T13:00:00Z"
TAGS = {T1: "MOMENTUM-002", T2: "BCTROT-004"}
_tag = lambda f: TAGS.get(f["transaction_time"])            # noqa: E731
FILLS = [_fill("AEM", "buy", 10, 100.0, T1), _fill("WPM", "buy", 5, 50.0, T2)]

PASSES = _certified()
FAILS = _certified(disagreements=(Disagreement("MOMENTUM-002", "AEM", "quantity", 10.0, 7.0),))

_MARKS = lambda d: {"AEM": 110.0, "WPM": 55.0}              # noqa: E731
_TS = lambda d: 1_700_000_000                               # noqa: E731


#: THE DEFAULT RESOLVER QUALIFIES, it does not pass through. Review demonstrated the identity
#: default live: with it, `session_fill_qty` was 0.0 on every row and every test stayed green,
#: because the default produced the one value under which the wiring cannot be seen broken. Every
#: test here now runs in the production shape — the broker says "AEM", the row says "AEM.XNYS".
_RESOLVE = lambda sym: f"{sym}.XNYS"                        # noqa: E731


def _run(store, sessions, *, gate=PASSES, marks_for=_MARKS, ledger_start="2026-08-01",
         instrument_of=_RESOLVE, **kw):
    return asyncio.run(backfill_sessions(
        store, FILLS, sessions, gate=gate, marks_for=marks_for, strategy_of=_tag,
        instrument_of=instrument_of,
        currency="USD", ledger_start=ledger_start, snapshot_ts_for=_TS, **kw,
    ))


# ==================================================================================================
# THE GATE IS A PRECONDITION
# ==================================================================================================
def test_a_FAILED_gate_writes_ABSOLUTELY_NOTHING():
    """Not "writes fewer rows", not "writes and flags" — nothing. A gate failure means the derivation
    itself is wrong, so every day it would write is wrong the same way, and history has nothing to be
    checked against afterwards. This is the last place it can be caught."""
    store = _Store()
    report = _run(store, ["2026-08-21", "2026-08-25"], gate=FAILS)
    assert store.calls == [], "the store must not be touched at all when the gate fails"
    assert report.written == 0
    assert report.sessions == ()
    assert "NOTHING WRITTEN" in report.summary


def test_an_INCONCLUSIVE_gate_is_also_a_refusal_and_not_a_pass():
    """`compared == 0` is the vacuity case: an empty book or a cache that returned nothing. It must
    not open the write path — a gate that succeeds because it examined nothing is worse than none."""
    store = _Store()
    report = _run(store, ["2026-08-21"], gate=AgreementReport(compared=0, engine_pairs=0))
    assert store.calls == []
    assert report.written == 0


def test_the_fixture_gate_can_actually_PASS_or_the_tests_below_prove_nothing():
    """FIXTURE PROPERTY. Every test after this one depends on PASSES opening the write path; if it
    could not, they would all "pass" by writing nothing for the wrong reason."""
    assert PASSES.agrees and not FAILS.agrees


# ==================================================================================================
# WHAT IT WRITES
# ==================================================================================================
def test_it_writes_reconstructed_rows_with_the_provenance_that_says_what_they_ARE():
    """A reconstructed row stamped `close` would put a rebuilt figure into a close-to-close series
    indistinguishably. The store REFUSES that combination; this pins that the runner never asks."""
    store = _Store()
    report = _run(store, ["2026-08-25"])
    rows, manifests = store.calls[0]
    assert report.written == len(rows) > 0
    assert {r["capture_kind"] for r in rows} == {CAPTURE_KIND}
    assert {r["provenance"] for r in rows} == {PROVENANCE}
    assert {r["mark_source"] for r in rows} == {MARK_SOURCE}
    assert {m["status"] for m in manifests} == {"observed"}


def test_the_book_GROWS_as_the_sessions_advance_which_is_the_whole_claim():
    """The reconstruction's only real claim is that truncating the ledger at a past instant gives
    that instant's book. On 08-21 only the AEM fill has happened; by 08-25 both have. A runner that
    passed the same date to every session — or ignored `as_of` — would write identical books and
    every other assertion here would still hold."""
    store = _Store()
    _run(store, ["2026-08-21", "2026-08-25"])
    first, second = (rows for rows, _ in store.calls)
    assert {r["instrument_id"] for r in first} == {"AEM.XNYS"}
    assert {r["instrument_id"] for r in second} == {"AEM.XNYS", "WPM.XNYS"}


def test_session_fill_qty_is_MEASURED_not_left_at_the_zero_default():
    """Zero on every historical row is a specific false statement — "nothing was traded that day" —
    not a missing one. The AEM buy lands on 08-20, so 08-20's row carries +10 and 08-25's carries 0
    for a position that was merely still held."""
    store = _Store()
    _run(store, ["2026-08-20", "2026-08-25"])
    day_of, later = (rows for rows, _ in store.calls)
    assert [r["session_fill_qty"] for r in day_of if r["instrument_id"] == "AEM.XNYS"] == [10.0]
    assert [r["session_fill_qty"] for r in later if r["instrument_id"] == "AEM.XNYS"] == [0.0]


def test_session_fill_qty_follows_the_LOT_not_the_FILL_when_ANOTHER_LANE_does_the_selling():
    """TWO DEFECTS IN ONE FIELD, both found by review, both invisible to my integration read-back
    because every fill there was same-lane.

    (1) ATTRIBUTION. The first version keyed each fill by the FILL's own tag. But position deltas
    move by the LOT's OPENING tag — that is #292's whole design and what `_match` walks. MOMENTUM
    opens 10 AEM; MANUAL sells 4. MOMENTUM's position drops to 6, and the -4 belongs on MOMENTUM's
    row; keyed by the fill it landed under (MANUAL, AEM), a key with no observation row, where
    `observation_rows`' `.get(..., 0.0)` silently discarded it. MOMENTUM's row then read 0 on the day
    it sold 4 — and the documented invariant `qty_t == qty_{t-1} + session_fill_qty_t + transfers_t`,
    which exists to detect corporate actions, was violated by an ordinary cross-lane sell. That is
    TECHIVOL's incident shape, not a hypothetical.

    (2) NAMESPACE. `session_fills` keyed on the bare `AEM` while the rows key on the resolved
    `AEM.XNYS`, so after the B2 fix the lookup missed on EVERY row and every historical
    session_fill_qty would have been 0.0. Two independent bugs, one field, both hidden by the same
    `.get(default)`.

    The fix deletes the second derivation rather than correcting it: the delta between the book at D
    and the book at D-1 IS the matcher's own answer, in the matcher's own namespace, attributed the
    matcher's own way. Two derivations of one fact drift; one cannot.
    """
    fills = [_fill("AEM", "buy", 10, 100.0, T1),                              # MOMENTUM opens
             _fill("AEM", "sell", 4, 130.0, "2026-08-26T13:00:00Z")]          # MANUAL sells
    tags = {T1: "MOMENTUM-002", "2026-08-26T13:00:00Z": "MANUAL"}
    store = _Store()
    asyncio.run(backfill_sessions(
        store, fills, ["2026-08-26"],
        gate=AgreementReport(compared=1, engine_pairs=1, activities_fingerprint=fingerprint(fills)),
        marks_for=_MARKS,
        strategy_of=lambda f: tags.get(f["transaction_time"]),
        currency="USD", ledger_start="2026-08-01", snapshot_ts_for=_TS,
        instrument_of=_RESOLVE,
    ))
    rows, _ = store.calls[0]
    mom = [r for r in rows if r["strategy_id"] == "MOMENTUM-002"]

    # FIXTURE PROPERTY FIRST: the seller really is a different lane from the opener, or this test is
    # the same-lane case that already passed.
    assert tags[T1] != tags["2026-08-26T13:00:00Z"]
    assert mom and mom[0]["qty"] == 6.0, "the lot is MOMENTUM's; the sell reduces MOMENTUM"
    assert mom[0]["session_fill_qty"] == -4.0, (
        "the -4 must land on the lane whose LOT moved, not on the lane that submitted the fill"
    )


def test_session_fill_qty_is_keyed_in_the_SAME_namespace_as_the_row_it_lands_on():
    """The second defect above, pinned on its own so a fix to one cannot mask the other. A lookup
    that misses returns the 0.0 default and looks exactly like a day nothing traded."""
    store = _Store()
    _run(store, ["2026-08-20"], instrument_of={"AEM": "AEM.XNYS", "WPM": "WPM.XNYS"}.get)
    rows, _ = store.calls[0]
    assert rows and rows[0]["instrument_id"] == "AEM.XNYS"
    assert rows[0]["session_fill_qty"] == 10.0, "resolved rows must still find their delta"


def test_a_lane_that_held_NOTHING_still_gets_a_manifest_row():
    """Absence must never be readable as flatness — and the inverse: a flat lane must be visibly
    flat rather than silently missing. `known_lanes` is what makes an unobserved day distinguishable
    from an empty one."""
    store = _Store()
    _run(store, ["2026-08-25"], known_lanes=("MOMENTUM-002", "BCTROT-004", "TECHIVOL-005"))
    _, manifests = store.calls[0]
    flat = [m for m in manifests if m["strategy_id"] == "TECHIVOL-005"]
    assert flat and flat[0]["instrument_count"] == 0 and flat[0]["status"] == "observed"


def test_an_UNPRICED_position_is_COUNTED_and_the_row_still_records_the_HOLDING():
    """The holding is a fact; the valuation is not. A day where a symbol has no bar must still record
    what was held — dropping the row would lose a real position because a price was missing, which is
    the fallback-is-a-silent-wrong-answer shape."""
    store = _Store()
    report = _run(store, ["2026-08-25"], marks_for=lambda d: {"AEM": 110.0})   # WPM unpriced
    rows, _ = store.calls[0]
    wpm = [r for r in rows if r["instrument_id"] == "WPM.XNYS"]
    assert wpm and wpm[0]["qty"] == 5.0, "the holding must survive a missing mark"
    assert wpm[0]["mark_px"] is None and wpm[0]["mark_source"] is None
    assert report.sessions[0].unpriced == 1


# ==================================================================================================
# WHAT IT REFUSES
# ==================================================================================================
def test_a_session_BEFORE_the_ledger_is_REFUSED_rather_than_recorded_as_every_lane_flat():
    """THE DEFECT THIS RUNNER EXISTS TO NOT COMMIT. Reconstructing before the ledger's coverage
    returns an EMPTY book, which is indistinguishable from a genuinely flat day once it is a row in a
    table. No row is the correct record of a day nobody could observe."""
    store = _Store()
    report = _run(store, ["2026-07-15", "2026-08-25"], ledger_start="2026-08-01")
    assert [s.session_date for s in report.refused] == ["2026-07-15"]
    assert "would report an empty book" in report.refused[0].reason
    assert len(store.calls) == 1, "only the in-range session may reach the store"
    assert {r["session_date"] for r in store.calls[0][0]} == {"2026-08-25"}


def test_ledger_start_is_TAKEN_not_INFERRED_from_the_earliest_fill():
    """A ledger truncated by a lookback window would report its own truncation point as the account's
    inception — the inference is most confident exactly where it is most wrong. So a caller stating a
    coverage EARLIER than any fill is honoured (those days really were flat), and this proves the
    runner is not quietly using `min(transaction_time)` instead."""
    store = _Store()
    report = _run(store, ["2026-08-05"], ledger_start="2026-08-01")
    assert report.refused == (), "08-05 is inside the stated coverage even though no fill precedes it"
    rows, manifests = store.calls[0]
    assert rows == [], "genuinely flat: covered by the ledger, nothing held yet"
    assert manifests == [] or all(m["instrument_count"] == 0 for m in manifests)


def test_a_marks_source_that_RAISES_refuses_the_day_and_says_which_error():
    """Raising and returning nothing are different states. Returning `{}` says "no bar for that
    symbol", recorded per-position as an unknown valuation; raising says we do not know whether bars
    exist at all. Base rows are append-only, so a day written with a silently degraded valuation
    could never be corrected — and a refusal costs nothing, because a re-run after the bar source is
    fixed writes normally."""
    store = _Store()

    def _boom(session_date):
        raise TimeoutError("the bar service did not answer")

    report = _run(store, ["2026-08-25"], marks_for=_boom)
    assert store.calls == []
    assert report.refused[0].action == "refused"
    assert "TimeoutError" in report.refused[0].reason and "unknown whether bars exist" in report.refused[0].reason


def test_a_marks_source_returning_NOTHING_still_writes_the_holdings():
    """The other side of the state above, and the reason they must not collapse. An empty dict is a
    real answer about prices, not a failure to answer."""
    store = _Store()
    report = _run(store, ["2026-08-25"], marks_for=lambda d: {})
    rows, _ = store.calls[0]
    assert rows and all(r["mark_px"] is None for r in rows)
    assert report.sessions[0].action == "written"


# ==================================================================================================
# RE-RUNS
# ==================================================================================================
def test_a_RERUN_reports_already_present_rather_than_a_clean_write_of_zero_rows():
    """`wrote 0 rows` is three facts — flat, already there, or refused — and an integer cannot tell
    them apart. The store returns the skip count precisely so the runner can, and a runner that threw
    it away would rebuild the ambiguity one level up."""
    store = _Store(already_present=99)          # everything conflicts
    report = _run(store, ["2026-08-25"])
    assert report.sessions[0].action == "already-present"
    assert report.sessions[0].rows_written == 0 and report.sessions[0].rows_skipped > 0
    assert report.written == 0


def test_the_runner_RESOLVES_the_brokers_symbol_into_the_ENGINES_namespace():
    """The rows must key the way every other `instrument_id` column in the live database does —
    `trade_cycle`, `position_claim_event`, `manager` and `watchlist_item` all hold `AEM.XNYS`, and
    #699's read path keys on it. A bare `AEM` here is a value that joins to nothing forever while
    looking perfectly fine in the row."""
    store = _Store()
    _run(store, ["2026-08-25"], instrument_of={"AEM": "AEM.XNYS", "WPM": "WPM.XNYS"}.get)
    rows, _ = store.calls[0]
    assert {r["instrument_id"] for r in rows} == {"AEM.XNYS", "WPM.XNYS"}


def test_a_symbol_the_runner_CANNOT_resolve_is_reported_as_DEGRADED_not_written_bare():
    """And the day is still recorded — the manifest says `degraded` and names the gap, because a
    capture that silently dropped a held position while reporting a clean `observed` is the
    absence-as-flatness defect one seam out."""
    store = _Store()
    report = _run(store, ["2026-08-25"], instrument_of={"AEM": "AEM.XNYS"}.get)   # WPM unresolvable
    rows, manifests = store.calls[0]
    assert {r["instrument_id"] for r in rows} == {"AEM.XNYS"}, "the bare symbol must not be stored"
    assert {m["status"] for m in manifests} == {"degraded"}
    assert any("could not be resolved" in (m["detail"] or "") for m in manifests)
    assert report.sessions[0].action == "written"


def test_a_session_exactly_ON_ledger_start_is_INCLUDED():
    """R1, a surviving mutant: `<` and `<=` on the coverage boundary were indistinguishable because
    no fixture put a session exactly on `ledger_start`. `ledger_start` is the first date the ledger
    COVERS, so that day is inside it — excluding it would silently drop the account's first session,
    which is the one day most likely to hold an opening position nobody re-buys later."""
    store = _Store()
    report = _run(store, ["2026-08-20"], ledger_start="2026-08-20")
    assert report.refused == (), f"the first covered day was refused: {report.sessions}"
    assert store.calls, "the boundary session must reach the store"


def test_a_PARTIALLY_present_rerun_is_its_OWN_state_and_not_reported_as_a_clean_write():
    """The comment promised three states and the ternary delivered two: ALL-present mapped to
    `already-present`, and SOME-present fell through to `written` — the exact ambiguity the comment
    said must not exist, with only the reason string carrying it. Prose and code disagreeing inside
    one expression.

    Partial presence is a real surprise (two captures racing for one slot, or a re-run after a
    method_version change that only some rows carry) and it deserves its own name."""
    store = _Store(already_present=1)          # one of this session's rows already exists
    report = _run(store, ["2026-08-25"])
    s = report.sessions[0]
    assert s.rows_written > 0 and s.rows_skipped > 0, "the fixture must actually be partial"
    assert s.action == "partially-present", f"got {s.action!r}"


def test_the_day_over_day_delta_is_computed_against_the_PREVIOUS_DAY_not_the_previous_LIST_ENTRY():
    """Sessions are not contiguous — a weekend, a holiday, or a caller backfilling only Fridays. The
    prior book must be "everything through the day before this session", never "whatever session came
    before in the list", or a Monday's delta would swallow the whole preceding week."""
    fills = [_fill("AEM", "buy", 10, 100.0, "2026-08-20T13:00:00Z"),
             _fill("AEM", "buy", 5, 110.0, "2026-08-24T13:00:00Z")]
    tag = lambda f: "MOMENTUM-002"        # noqa: E731
    d = session_deltas(fills, "2026-08-24", strategy_of=tag, instrument_of={"AEM": "AEM.XNYS"}.get)
    assert d == {("MOMENTUM-002", "AEM.XNYS"): 5.0}, (
        f"only the 08-24 buy belongs to the 08-24 session; got {d}"
    )


def test_the_backfill_REFUSES_a_gate_certified_against_DIFFERENT_activities():
    """The gate validates the LEDGER'S REACH — that is the one thing no per-day inspection could
    establish, because a fill history that does not reach account inception makes every reconstructed
    day wrong invisibly. That validation is worthless if the certified ledger and the written-from
    ledger can differ, so the report carries a fingerprint and this checks it.

    No mistake is visible in this call: the gate honestly agreed, about a ledger that is not this
    one."""
    other = AgreementReport(compared=1, engine_pairs=1,
                            activities_fingerprint="certified-against-something-else")
    store = _Store()
    report = _run(store, ["2026-08-25"], gate=other)
    assert store.calls == [], "nothing may be written against an uncertified ledger"
    assert report.written == 0
    assert "different set of activities" in report.summary


def test_a_gate_with_NO_fingerprint_is_REFUSED_rather_than_trusted():
    """Three states: certified-for-these, certified-for-others, and never-certified. A hand-built
    report carries no fingerprint, and absence must not read as permission — that is the whole
    subject of this table."""
    store = _Store()
    report = _run(store, ["2026-08-25"], gate=AgreementReport(compared=1, engine_pairs=1))
    assert store.calls == []
    assert "was not produced by a gate run" in report.summary


def test_the_runner_REFUSES_to_be_called_without_a_resolver_at_all():
    """A default of None means IDENTITY, and identity is the one value under which this wiring cannot
    be seen broken: review drove the runner with a real resolver against a gate that had one, watched
    every row store `AEM.XNYS` with `session_fill_qty` 0.0, and every test stayed green because the
    tests omitted the argument too. The same reasoning already applies to `currency` three arguments
    up, which also has no default and for the same reason — a plausible value recorded where the
    caller never made a choice.

    A caller that genuinely wants identity writes `lambda s: s`, which is a decision."""
    with pytest.raises(TypeError, match="instrument_of"):
        asyncio.run(backfill_sessions(
            _Store(), FILLS, ["2026-08-25"], gate=PASSES, marks_for=_MARKS, strategy_of=_tag,
            currency="USD", ledger_start="2026-08-01", snapshot_ts_for=_TS,
        ))


def test_the_manifest_COUNTS_the_store_computed_reach_the_caller():
    """Computed-and-discarded, in the file whose own comments cite the rule against it. A run that
    wrote no observations but DID write manifests is a real outcome and a different one from a run
    that wrote neither — that is the whole reason the store returns them separately."""
    store = _Store()
    report = _run(store, ["2026-08-25"], known_lanes=("MOMENTUM-002", "BCTROT-004", "TECHIVOL-005"))
    s = report.sessions[0]
    assert s.manifests_written == s.lanes_attempted >= 3, (
        f"the store's manifest counts must reach the caller; got {s.manifests_written}/{s.lanes_attempted}"
    )


def test_a_fill_dated_on_a_day_NO_SESSION_covers_is_REPORTED_rather_than_vanishing():
    """T2, fixed rather than ticketed because it produces a FALSE SIGNAL, not just a gap.

    Each session's delta is `book(D) - book(D-1)`, which covers exactly the fills dated D. A fill
    stamped on a day that is in no session's walk — a weekend- or holiday-dated correction, a bust,
    a venue-side adjustment — therefore appears in NO session's delta. It still moves the book, so
    the reader sees a quantity jump with `session_fill_qty` 0 across the gap: precisely the shape
    `qty_t == qty_{t-1} + session_fill_qty_t + transfers_t` exists to flag as a corporate action.
    The column would be manufacturing the alarm it was added to make trustworthy.

    Nothing can fix the arithmetic without inventing a session that did not happen, so the run
    REPORTS it. A named gap is a different thing from a wrong number that looks right."""
    fills = [_fill("AEM", "buy", 10, 100.0, "2026-08-22T13:00:00Z")]      # a Saturday
    store = _Store()
    report = asyncio.run(backfill_sessions(
        store, fills, ["2026-08-21", "2026-08-24"],                       # Fri and Mon
        gate=AgreementReport(compared=1, engine_pairs=1, activities_fingerprint=fingerprint(fills)),
        marks_for=_MARKS, strategy_of=lambda f: "MOMENTUM-002", instrument_of=_RESOLVE,
        currency="USD", ledger_start="2026-08-01", snapshot_ts_for=_TS,
    ))
    # FIXTURE PROPERTY FIRST: the fill really does fall between the two sessions and really does
    # move the book, or there is nothing for the report to have missed.
    rows_mon = [r for r in store.calls[-1][0] if r["instrument_id"] == "AEM.XNYS"]
    assert rows_mon and rows_mon[0]["qty"] == 10.0, "the fill must actually be in Monday's book"
    assert rows_mon[0]["session_fill_qty"] == 0.0, "and contribute to no session's delta"

    assert "2026-08-22" in report.uncovered_fill_dates, (
        f"the off-session fill date must be reported; got {report.uncovered_fill_dates}"
    )
    assert "not covered by any session" in report.summary


def test_a_ledger_whose_fills_all_land_ON_sessions_reports_NO_gap():
    """The other half, so the detector cannot pass by always crying. A report that flagged every run
    would be muted within a week — the notifier-death pattern this repo has already paid for."""
    store = _Store()
    report = _run(store, ["2026-08-20", "2026-08-21"])
    assert report.uncovered_fill_dates == (), report.uncovered_fill_dates
    assert "not covered" not in report.summary


def test_fills_OUTSIDE_the_walked_range_are_not_reported_as_gaps():
    """A backfill of one week must not complain about every fill in the other five. Only dates
    BETWEEN the first and last walked session can be gaps — outside that range there is no claim of
    coverage to violate, and reporting them would be the crying-detector above."""
    store = _Store()
    report = _run(store, ["2026-08-24", "2026-08-25"])       # FILLS has one on 08-20, before the walk
    assert report.uncovered_fill_dates == (), report.uncovered_fill_dates


def test_session_deltas_ALSO_refuses_an_absent_resolver():
    """T3. Harmless inside `backfill_sessions`, which always forwards one — but this is a public
    function, and a standalone import defaulting to identity would silently produce bare-keyed
    deltas that match no row. Same treatment as the runner, for the same reason: the default value
    is the one under which the mistake is invisible."""
    with pytest.raises(TypeError, match="instrument_of"):
        session_deltas(FILLS, "2026-08-20", strategy_of=_tag)
