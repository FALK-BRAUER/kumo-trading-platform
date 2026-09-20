"""The gate that must pass before any reconstructed history is trusted (#734).

WHY IT EXISTS. Reconstruction derives a lane's book from the broker's fills; the engine derives it
from Nautilus's own cache. At `T = now` those describe the SAME instant by routes that share nothing,
so they must agree. If they disagree today, every historical row the backfill writes is wrong in the
same way — and nobody finds out, because history has nothing to be checked against.

PER-LANE, NOT PER-ACCOUNT. An account-level check cancels exactly the error most worth catching: a
lot attributed to the wrong lane leaves the account total untouched. Comparing totals would not be
testing attribution at all.

QUANTITY AND BASIS, NEVER UNREALIZED. The two sides price from different sources — the live cache
versus a historical bar — so comparing valuations would report a disagreement that is really just two
marks. Quantity and basis are what the reconstruction claims to recover.
"""

from __future__ import annotations

import pytest

from api.eod_backfill import AgreementReport, BASIS_TOLERANCE, fingerprint, verify_at_now


class _Px:
    def __init__(self, v):
        self._v = float(v)

    def as_double(self):
        return self._v


class _Position:
    """The Nautilus surface `observe_lanes` reads: strategy_id, instrument_id, signed_qty,
    avg_px_open, unrealized_pnl(Price)."""

    def __init__(self, lane, instrument, qty, avg_px):
        self.strategy_id = lane
        self.instrument_id = instrument
        self.signed_qty = qty
        self.avg_px_open = _Px(avg_px)

    def unrealized_pnl(self, price):
        if not hasattr(price, "as_double"):
            raise TypeError("Position.unrealized_pnl takes a Price, not a float")
        return _Px(0.0)


class _Unreadable:
    """A position whose quantity RAISES, which is how production produces a `skipped` entry.

    The first version of this test set `skipped` on the cache double and asserted the gate read it —
    and it failed, correctly: `observe_lanes` COMPUTES that list, it does not accept one. A double
    that declares the state instead of causing it is testing my belief about the observer rather than
    the observer. `signed_qty` is a property on the real Position, so this raises on ACCESS."""

    strategy_id = "BCTROT-004"
    instrument_id = "WPM.XNYS"
    avg_px_open = _Px(50.0)

    @property
    def signed_qty(self):
        raise RuntimeError("the venue sent a quantity this position cannot express")

    def unrealized_pnl(self, price):
        raise RuntimeError("unreachable: the quantity failed first")


class _Cache:
    def __init__(self, positions):
        self._positions = positions

    def positions_open(self):
        return list(self._positions)

    def price(self, instrument_id, price_type):
        return _Px(100.0)


def _fill(sym, side, qty, price, t):
    return {"symbol": sym, "side": side, "qty": str(qty), "price": str(price), "transaction_time": t}


#: "Now" as a whole trading DATE. `reconstruct_lanes` refuses anything finer, because the string
#: ordering these timestamps are compared under stops being chronological once fractional precision
#: varies — and a partial date silently selects the wrong fills entirely.
NOW = "2026-08-29"
#: REAL ISO-8601 TIMESTAMPS, because the truncation is a STRING comparison against `as_of`. A first
#: version used T1/T2 as transaction_times and every fill was silently excluded ("t" > "2"
#: lexically), so the reconstruction returned an empty book and the agreement tests failed for a
#: reason that had nothing to do with the gate. A double that cannot represent production is the bug.
T1 = "2026-08-20T13:00:00Z"
T2 = "2026-08-21T13:00:00Z"
UNTAGGED = "2026-07-01T13:00:00Z"
TAGS = {T1: "MOMENTUM-002", T2: "BCTROT-004"}
_tag = lambda f: TAGS.get(f["transaction_time"])        # noqa: E731


# ==================================================================================================
# THE GATE MUST BE ABLE TO FAIL
# ==================================================================================================
def test_NOTHING_COMPARED_is_INCONCLUSIVE_and_explicitly_not_a_pass():
    """The vacuity this repo keeps finding. An empty book, or a cache that returned nothing, must not
    read as "the reconstruction checks out" — a gate that succeeds because it examined nothing is
    worse than no gate, because it produces a receipt."""
    report = verify_at_now(_Cache([]), [], as_of=NOW, strategy_of=_tag)
    assert report.compared == 0
    assert report.agrees is False
    assert "not a pass" in report.verdict


def test_the_fixture_can_produce_a_REAL_agreement():
    """And the other half: when both sides genuinely describe the same book, the gate says so. Without
    this the failing tests below could all be passing for the wrong reason."""
    cache = _Cache([_Position("MOMENTUM-002", "AEM", 10, 100.0)])
    fills = [_fill("AEM", "buy", 10, 100.0, T1)]
    report = verify_at_now(cache, fills, as_of=NOW, strategy_of=_tag)
    assert report.agrees, report.verdict
    assert report.compared == 1


# ==================================================================================================
# WHAT IT MUST CATCH
# ==================================================================================================
def test_a_QUANTITY_mismatch_is_caught():
    cache = _Cache([_Position("MOMENTUM-002", "AEM", 10, 100.0)])
    fills = [_fill("AEM", "buy", 7, 100.0, T1)]
    report = verify_at_now(cache, fills, as_of=NOW, strategy_of=_tag)
    assert not report.agrees
    assert report.disagreements[0].what == "quantity"
    assert (report.disagreements[0].engine, report.disagreements[0].reconstructed) == (10.0, 7.0)


def test_a_BASIS_divergence_is_RECORDED_and_never_GATED_ON():
    """THE GATE MUST NOT COMPARE BASIS, and this test used to assert that it did.

    The two sides use different, both-correct conventions. Nautilus's `avg_px_open` is the weighted
    average of the OPENING fills and does not move when part of the position is closed; the FIFO
    residue drops the oldest lots. Measured against the installed package: buy 10@100, buy 10@120,
    sell 10 gives `avg_px_open` 110.0 and a FIFO residue of 120.0 — same fills, both right.

    So any position with a partial close after multi-price entries would have failed this gate with
    NO defect present, and the live book contains exactly those. The divergence is structural and
    unbounded; recording the pair is the #370 move, tolerance-fudging it would be a lie.
    """
    cache = _Cache([_Position("MOMENTUM-002", "AEM", 10, 110.0)])       # engine: opening-weighted
    fills = [                                                          # FIFO residue: 120.0
        _fill("AEM", "buy", 10, 100.0, T1),
        _fill("AEM", "buy", 10, 120.0, T2),
        _fill("AEM", "sell", 10, 130.0, "2026-08-22T13:00:00Z"),
    ]
    tags = {T1: "MOMENTUM-002", T2: "MOMENTUM-002", "2026-08-22T13:00:00Z": "MOMENTUM-002"}
    report = verify_at_now(cache, fills, as_of=NOW, strategy_of=lambda f: tags.get(f["transaction_time"]))

    # FIXTURE PROPERTY FIRST: the two bases genuinely differ, or this test proves nothing.
    assert report.basis_divergences, "the fixture must actually produce divergent bases"
    d = report.basis_divergences[0]
    assert (d.engine_avg_px_open, d.fifo_residue_basis) == (110.0, 120.0)

    # And the gate still PASSES: the quantities agree, which is what it actually verifies.
    assert report.agrees, report.verdict



def test_a_lot_attributed_to_the_WRONG_LANE_is_caught_even_though_the_account_total_is_unchanged():
    """THE ERROR AN ACCOUNT-LEVEL CHECK CANNOT SEE, and the reason this gate is per-lane. The book
    holds 15 AEM either way; only the split differs, and the split is the entire claim the
    reconstruction makes."""
    cache = _Cache([
        _Position("MOMENTUM-002", "AEM", 10, 100.0),
        _Position("BCTROT-004", "AEM", 5, 100.0),
    ])
    # The fills say BCTROT opened both lots — same total, wrong owner.
    swapped = {T1: "BCTROT-004", T2: "BCTROT-004"}
    fills = [_fill("AEM", "buy", 10, 100.0, T1), _fill("AEM", "buy", 5, 100.0, T2)]
    report = verify_at_now(cache, fills, as_of=NOW, strategy_of=lambda f: swapped.get(f["transaction_time"]))
    assert not report.agrees, "a mis-attributed lot must not pass because the account total matches"


def test_a_position_ONE_SIDE_holds_and_the_other_does_not_is_caught():
    cache = _Cache([_Position("MOMENTUM-002", "AEM", 10, 100.0)])
    report = verify_at_now(cache, [], as_of=NOW, strategy_of=_tag)
    assert not report.agrees
    assert report.disagreements[0].what == "held by only one derivation"


# ==================================================================================================
# THE ONE EXEMPTION, AND ITS LIMIT
# ==================================================================================================
def test_ANY_unattributed_lot_at_T_NOW_is_a_FAILURE_not_an_exemption():
    """THIS TEST USED TO ASSERT THE EXEMPTION, which was the hole.

    The whole live book postdates 2026-08-17, where the fill-to-order join is measured at 100%. So a
    lot the reconstruction cannot attribute TODAY does not mean "old money" — it means the join
    broke. Exempting it would let the failure this gate exists for pass silently, and the review
    constructed exactly that: a lane whose join fails appears as UNCLAIMED, its engine key is never
    examined, and the gate reports success.

    Exceptions must be named and dated by the caller, never assumed by the gate.
    """
    cache = _Cache([_Position("MOMENTUM-002", "AEM", 10, 100.0)])
    fills = [
        _fill("AEM", "buy", 10, 100.0, T1),
        _fill("WPM", "buy", 7, 50.0, UNTAGGED),          # join failed -> UNCLAIMED
    ]
    report = verify_at_now(cache, fills, as_of=NOW, strategy_of=_tag)
    assert not report.agrees
    assert any("unattributed" in d.what for d in report.disagreements)



def test_the_exemption_does_NOT_hide_a_position_the_ENGINE_attributes():
    """THE LIMIT OF THE EXEMPTION, and the case review asked for. If the engine holds a position that
    the reconstruction could only put in UNCLAIMED, that IS an attribution failure — the engine knows
    the owner and the reconstruction lost it. Dropping it with the exemption would let exactly the
    failure this gate exists for hide inside the exemption."""
    cache = _Cache([_Position("MOMENTUM-002", "WPM", 7, 50.0)])
    fills = [_fill("WPM", "buy", 7, 50.0, UNTAGGED)]      # reconstruction can only say UNCLAIMED
    report = verify_at_now(cache, fills, as_of=NOW, strategy_of=_tag)
    assert not report.agrees, (
        "the engine attributed this position and the reconstruction did not — that is the failure "
        "this gate exists to catch, and it must not be absorbed by the UNCLAIMED exemption"
    )


# ==================================================================================================
# THE NAMESPACE — the reason this gate could never have passed in production
# ==================================================================================================
def test_the_gate_AGREES_when_the_engine_speaks_INSTRUMENT_IDS_and_the_fills_speak_SYMBOLS():
    """B2, and the defect four review rounds missed because BOTH sides of the double said "AEM".

    The engine keys on `str(position.instrument_id)`, which a Nautilus InstrumentId renders as
    `AEM.XNYS`. The broker's fill ledger carries a bare `symbol`, `AEM`. Measured, not assumed:
    `str(InstrumentId.from_str("AEM.XNAS"))` is `'AEM.XNAS'` and `.symbol` is `'AEM'`.

    So the two key sets were DISJOINT for every real position: every one would be reported twice as
    "held by only one derivation", `agrees` would be False forever, and the backfill would be
    permanently refused. It fails CLOSED — no wrong row could land — but a gate that structurally
    cannot accept is dead code wearing a safety label, and the hazard is the `.split(".")[0]` someone
    bolts on the morning it first matters.

    The live database settles which namespace wins: `trade_cycle`, `position_claim_event`, `manager`
    and `watchlist_item` all store `AEM.XNYS`, and #699's read path keys on `instrument_id`. So the
    RECONSTRUCTION resolves upward, at one seam, and the engine stays native.
    """
    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10, 100.0)])
    fills = [_fill("AEM", "buy", 10, 100.0, T1)]                 # the broker says "AEM"

    # FIXTURE PROPERTY FIRST: the two sides really are in different namespaces, or this proves
    # nothing. This is the assertion whose absence let a same-string double hide the defect.
    assert str(cache.positions_open()[0].instrument_id) != fills[0]["symbol"]

    report = verify_at_now(cache, fills, as_of=NOW, strategy_of=_tag,
                           instrument_of={"AEM": "AEM.XNYS"}.get)
    assert report.agrees, report.verdict
    assert report.compared == 1


def test_a_symbol_the_resolver_CANNOT_place_is_a_DISAGREEMENT_not_a_bare_row():
    """Storing `AEM` in a column every other table fills with `AEM.XNYS` is a value that joins to
    nothing, forever, and looks perfectly fine in the row. Refuse instead."""
    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10, 100.0)])
    fills = [_fill("AEM", "buy", 10, 100.0, T1)]
    report = verify_at_now(cache, fills, as_of=NOW, strategy_of=_tag,
                           instrument_of=lambda sym: None)          # resolves nothing
    assert not report.agrees
    assert any("could not be resolved" in d.what for d in report.disagreements), report.disagreements


# ==================================================================================================
# G3 WAS A DETECTOR THAT COULD NEVER FIRE
# ==================================================================================================
def test_an_INCOMPLETE_report_is_not_a_pass():
    """MUTATION EVIDENCE: deleting `compared >= engine_pairs` from `agrees` left all 22 tests green,
    and so did `engine_pairs=0`. The conjunct was structurally unreachable through `verify_at_now` —
    every engine key survives into `keys` by construction — so it was a guard that could not fire and
    a verdict branch nothing could reach.

    It is kept because the dataclass is public and a future caller could build one, and it is now
    tested DIRECTLY. `verify_at_now` asserts the invariant instead of branching on it."""
    r = AgreementReport(compared=2, engine_pairs=3)
    assert not r.agrees and "INCOMPLETE" in r.verdict and "non-empty is not complete" in r.verdict


def test_verify_at_now_ASSERTS_that_every_engine_key_was_examined():
    """The invariant the unreachable conjunct was groping for. An assertion says "this cannot happen
    and here is where you find out if it ever does"; a branch says "this happens sometimes", which was
    false and made the branch dead."""
    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10, 100.0)])
    report = verify_at_now(cache, [_fill("AEM", "buy", 10, 100.0, T1)], as_of=NOW,
                           strategy_of=_tag, instrument_of={"AEM": "AEM.XNYS"}.get)
    assert report.compared >= report.engine_pairs == 1


# ==================================================================================================
# WHAT THE ENGINE COULD NOT DESCRIBE MUST NOT BE CERTIFIED AS AGREEMENT
# ==================================================================================================
def test_a_position_the_OBSERVER_could_not_describe_blocks_the_gate():
    """F4. `observe_lanes` can say "I could not describe N positions" — an unreadable quantity, a
    position whose properties raise. The gate would then certify agreement on a PARTIALLY READ book,
    and if the fill ledger also lacks those positions (an adopted or transferred position with no
    fills is the concrete case) it reads GREEN while the engine itself recorded the gap.

    Assert on the RECORD of the attempt, not on what happened to survive it."""
    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10, 100.0), _Unreadable()])
    report = verify_at_now(cache, [_fill("AEM", "buy", 10, 100.0, T1)], as_of=NOW,
                           strategy_of=_tag, instrument_of={"AEM": "AEM.XNYS"}.get)
    # FIXTURE PROPERTY FIRST: the observer must actually have skipped something, or the assertion
    # below is about nothing at all.
    from api.eod_observer import observe_lanes
    assert observe_lanes(cache).skipped, "the fixture did not produce an undescribable position"

    assert not report.agrees, "the book was only partially read; that is not agreement"
    assert any("could not be described" in d.what for d in report.disagreements)


def test_TWO_positions_colliding_on_ONE_key_is_caught_rather_than_silently_overwritten():
    """`_by_key` is a dict comprehension: a duplicate (lane, instrument) pair would drop one position
    and the gate would compare the survivor. Unreachable under NETTING — one net position per
    strategy-instrument — but an assertion costs a line and closes it forever."""
    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10, 100.0),
                    _Position("MOMENTUM-002", "AEM.XNYS", 5, 100.0)])
    with pytest.raises(AssertionError, match="one net position"):
        verify_at_now(cache, [], as_of=NOW, strategy_of=_tag, instrument_of=lambda s: s)


# ==================================================================================================
# THE REPORT IS A CERTIFICATE ABOUT SPECIFIC INPUTS, NOT A TOKEN
# ==================================================================================================
def test_the_report_FINGERPRINTS_the_activities_it_certified():
    """2a. The runner's docstring claimed "a caller cannot reach the write path without having
    produced one" — false as written: `AgreementReport(compared=1, engine_pairs=1)` takes no gate run
    at all, and my own PASSES fixture is exactly that.

    The realistic failure is worse than a fabricated report, because it involves no mistake anyone
    could see: verify the gate against fetch A (full history), then run the backfill against fetch B
    (truncated by a lookback window). `agrees` is HONESTLY true, every reconstructed day is wrong,
    and `ledger_start` cannot save you because the caller states it. The gate's entire value is that
    it validates the ledger's REACH — and that value evaporates if the certified ledger and the
    written-from ledger are allowed to be different objects."""
    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10, 100.0)])
    fills = [_fill("AEM", "buy", 10, 100.0, T1)]
    report = verify_at_now(cache, fills, as_of=NOW, strategy_of=_tag,
                           instrument_of={"AEM": "AEM.XNYS"}.get)
    assert report.agrees
    assert report.activities_fingerprint == fingerprint(fills)


def test_the_fingerprint_MOVES_when_the_ledger_is_TRUNCATED():
    """FIXTURE PROPERTY for the test above: a fingerprint that could not tell fetch A from fetch B
    would satisfy every assertion here while protecting nothing. Truncation is the specific
    difference that matters, so it is the one asserted."""
    full = [_fill("AEM", "buy", 10, 100.0, T1), _fill("WPM", "buy", 5, 50.0, T2)]
    truncated = full[1:]                                    # a lookback window that lost the opener
    assert fingerprint(full) != fingerprint(truncated)
    assert fingerprint(full) == fingerprint(list(reversed(full))), (
        "order must not matter — the same fills fetched in a different order are the same ledger"
    )


def test_the_fingerprint_covers_the_WHOLE_activity_not_just_what_the_MATCHER_reads():
    """T1, and the reason it is fixed rather than ticketed: it can produce a wrong row.

    The first version hashed transaction_time, symbol, side, qty and price — everything `_match`
    reads, and NOTHING the attribution reads. Strip the order-id field and the fingerprints are
    EQUAL (measured: True). So the gate certifies an id-bearing fetch, the backfill runs against an
    id-stripped one, `agrees` is honestly true, and every lot lands UNCLAIMED with coverage 0.0.

    The repair then DOUBLE-COUNTS rather than corrects: UNCLAIMED rows and per-lane rows differ in
    `strategy_id`, so `uq_eod_observation_base` keeps BOTH, and any per-instrument sum across lanes
    counts the position twice. Base rows are append-only by design, so that is not undoable.

    Hashing the whole activity makes a spurious mismatch fail CLOSED and loud, where the narrow
    version failed OPEN and silent. This repo picks that direction every time.
    """
    base = {"transaction_time": T1, "symbol": "AEM", "side": "buy", "qty": "10", "price": "100"}
    with_id = [{**base, "order_id": "abc-123"}]
    without = [dict(base)]

    # FIXTURE PROPERTY FIRST: the two ledgers really are identical in every MATCHED field, or this
    # test is about two obviously-different inputs and proves nothing.
    assert {k: v for k, v in with_id[0].items() if k in base} == without[0]
    assert fingerprint(with_id) != fingerprint(without), (
        "a field the attribution joins on must change the fingerprint, or the certificate says "
        "nothing about the ledger the lanes are derived from"
    )


def test_the_fingerprint_is_still_INSENSITIVE_TO_ORDER():
    """Kept from the narrow version and worth re-pinning after widening: the same fills fetched in a
    different order are the same ledger, and refusing that would be a false alarm the operator learns
    to route around — which is how a real detector gets muted."""
    a = {"transaction_time": T1, "symbol": "AEM", "side": "buy", "qty": "10", "order_id": "x"}
    b = {"transaction_time": T2, "symbol": "WPM", "side": "buy", "qty": "5", "order_id": "y"}
    assert fingerprint([a, b]) == fingerprint([b, a])


def test_the_fingerprint_ignores_KEY_ORDER_within_one_activity():
    """A dict built by a different code path with the same content is the same fill. Without this the
    certificate would depend on how the fetch happened to construct its dicts."""
    a = {"symbol": "AEM", "transaction_time": T1, "qty": "10"}
    b = {"qty": "10", "transaction_time": T1, "symbol": "AEM"}
    assert fingerprint([a]) == fingerprint([b])
