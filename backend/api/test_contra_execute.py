"""Between approving a repair and booking it, the book can move (#744).

`plan_contra_closes` decides WHAT to repair from a read taken at planning time. This module decides
whether that decision is still true against the book as it is NOW, immediately before any leg is
booked — and produces the exact legs, aimed at position ids READ from the live cache.

WHY A SECOND STAGE AT ALL. Every failure this repair can cause is a failure of aim. `_apply_leg`
hardcodes `PositionId(f"{instrument}-{strategy}")`; a fill aimed at an id that is not the real one
does not close the short, it OPENS A NEW POSITION — re-minting the exact defect being repaired, in a
lane that may hold nothing. A live cache read once found all sixteen legs matching the reconstructed
shape and only ONE was inspected deeply enough to confirm it. Being right by coincidence is not
being right, so the id is read, never rebuilt.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from api.contra_close import plan_contra_closes
from api.contra_execute import prepare_execution


@dataclass
class _Pos:
    instrument_id: str
    strategy_id: str
    signed_qty: float
    avg_px: float
    pos_id: str
    is_open: bool = True
    #: PRODUCTION CARRIES THIS and the fingerprint reads it — a double that lacked it would make the
    #: NETTING id-reuse check untestable, which is the shape this repo calls the double being the bug.
    ts_opened: int = 1_000

    @property
    def id(self):
        return self.pos_id

    @property
    def side(self):
        class _S:
            name = "LONG" if self.signed_qty > 0 else "SHORT"
        return _S()

    @property
    def avg_px_open(self):
        return self.avg_px


def _book(inst="CGAU.XNYS", long_qty=168.0, short_qty=-88.0,
          long_id=None, short_id=None):
    return [
        _Pos(inst, "BCTROT-004", long_qty, 41.10, long_id or f"{inst}-BCTROT-004"),
        _Pos(inst, "MOMENTUM-002", short_qty, 43.55, short_id or f"{inst}-MOMENTUM-002"),
    ]



#: The broker plane the fixtures are planned against. Passed explicitly at execution too, because
#: `prepare_execution` requires it: it is the plane that anchors the repair, and a defaulted one
#: would let a caller omit it and still get orders back.
_BROKER = {"CGAU.XNYS": 80.0}


def _exec(plan, positions, broker=None, **kw):
    return prepare_execution(plan, positions, _BROKER if broker is None else broker, **kw)


def _planned(book=None, broker=None):
    plan = plan_contra_closes(book or _book(), broker=broker or {"CGAU.XNYS": 80.0})
    assert plan.pairs, "fixture produced no pair — every assertion below would be vacuous"
    return plan


# ==================================================================================================
# THE HAPPY PATH, AND WHAT THE LEGS MUST BE
# ==================================================================================================
def test_both_legs_are_aimed_at_position_ids_READ_FROM_THE_LIVE_BOOK():
    """Not `f"{instrument}-{strategy}"`. That reconstruction is what re-mints the defect."""
    book = _book(long_id="POS-LONG-REAL", short_id="POS-SHORT-REAL")
    plan = _planned(book)
    # FIXTURE PROPERTY: the real ids must NOT equal the reconstructed shape, or a reconstructing
    # implementation would pass this test by coincidence — the exact coincidence that hid the risk.
    # DERIVED FROM THE FIXTURE, not written as two string literals: the literal form compared
    # "POS-LONG-REAL" against "CGAU.XNYS-BCTROT-004" and could never fail, so it silently stopped
    # describing the fixture the moment `_book`'s ids or instrument changed.
    for pos in book:
        assert pos.id != f"{pos.instrument_id}-{pos.strategy_id}", (
            "the fixture's ids match the reconstructed shape, so a reconstructing implementation "
            "would pass this test by coincidence"
        )

    ex = _exec(plan, book)
    assert ex.refused == (), ex.refused
    ids = {leg.position_id for leg in ex.orders[0].legs}
    assert ids == {"POS-LONG-REAL", "POS-SHORT-REAL"}, ids


def test_each_leg_fills_at_ITS_OWN_basis_so_neither_lane_realizes_anything():
    """The repair moves attribution, not money. A shared price would realize a gain on one lane and
    an equal loss on the other — inventing per-lane P&L out of a bookkeeping correction."""
    ex = _exec(_planned(), _book())
    by_lane = {leg.strategy_id: leg.price for leg in ex.orders[0].legs}
    assert by_lane == {"BCTROT-004": 41.10, "MOMENTUM-002": 43.55}


def test_the_short_is_BOUGHT_back_and_the_long_is_SOLD_down():
    """Direction is the difference between repairing the book and doubling the position."""
    ex = _exec(_planned(), _book())
    by_lane = {leg.strategy_id: (leg.side, leg.quantity) for leg in ex.orders[0].legs}
    assert by_lane == {"MOMENTUM-002": ("BUY", 88.0), "BCTROT-004": ("SELL", 88.0)}


def test_the_erased_realized_and_the_partial_flag_travel_WITH_the_order():
    """Both are things the repair CANNOT fix, and the ticket is explicit that they must be recorded
    rather than accepted silently — otherwise the lane sums and the broker plane disagree forever by
    exactly this amount with the reason discarded."""
    ex = _exec(_planned(), _book())
    o = ex.orders[0]
    assert o.unbooked_realized == pytest.approx((43.55 - 41.10) * 88.0)
    assert o.is_partial is True          # 168 long against an 88 short leaves 80 standing


# ==================================================================================================
# WHAT IT REFUSES — every one of these is a book that MOVED since approval
# ==================================================================================================
def test_a_pair_whose_QUANTITY_MOVED_is_refused_never_resized():
    """Approval was of a fixed list. Resizing to numbers nobody approved is the repair deciding for
    itself how much of a live book to rewrite."""
    plan = _planned()
    moved = _book(long_qty=150.0)        # the long shrank between approval and execution
    ex = _exec(plan, moved)
    assert ex.orders == ()
    assert len(ex.refused) == 1
    assert "moved" in ex.refused[0].reason or "fingerprint" in ex.refused[0].reason


def test_a_pair_whose_POSITION_ID_CHANGED_is_refused():
    """The id IS the aim. A changed id is the difference between closing the short and opening a new
    position, which is the defect this repair exists to undo."""
    ex = _exec(_planned(_book(short_id="OLD")), _book(short_id="NEW"))
    assert ex.orders == ()
    assert ex.refused


def test_a_leg_that_CLOSED_between_approval_and_execution_is_refused():
    """A closed position is not a smaller one. Booking a contra-leg against it opens a fresh
    position in a lane holding nothing — strictly worse than the state being repaired."""
    book = _book()
    book[1].is_open = False
    ex = _exec(_planned(), book)
    assert ex.orders == ()
    assert ex.refused


def test_a_leg_that_VANISHED_from_the_book_is_refused():
    ex = _exec(_planned(), [_book()[0]])
    assert ex.orders == ()
    assert ex.refused


def test_ONE_MOVED_PAIR_DOES_NOT_CANCEL_A_STILL_VALID_ONE():
    """Aim at the class: a per-pair decision must stay per-pair. An all-or-nothing refusal would make
    one drifting instrument block a repair that is still exactly correct for every other."""
    steady = _book("WHD.XNYS", 28.0, -28.0)
    drifting = _book("CGAU.XNYS", 168.0, -88.0)
    plan = plan_contra_closes(steady + drifting,
                              broker={"WHD.XNYS": 0.0, "CGAU.XNYS": 80.0})
    assert len(plan.pairs) == 2, "fixture must plan two pairs or this proves nothing"

    now = steady + _book("CGAU.XNYS", 150.0, -88.0)      # only CGAU moved
    ex = _exec(plan, now, {"WHD.XNYS": 0.0, "CGAU.XNYS": 80.0})
    assert [o.instrument_id for o in ex.orders] == ["WHD.XNYS"]
    assert [r.instrument_id for r in ex.refused] == ["CGAU.XNYS"]


def test_an_EMPTY_plan_produces_nothing_and_refuses_nothing():
    ex = _exec(plan_contra_closes([], broker={}), [], {})
    assert ex.orders == () and ex.refused == ()


def test_a_position_that_CANNOT_SAY_whether_it_is_open_is_refused_BY_NAME():
    """Absence must not read as permission, and the safe default is not enough on its own.

    A mutation flipping the openness default from False to True passed this entire file, because
    every fixture here carries `is_open`. The double could not represent the shape the guard exists
    for — so the guard was protected by nothing.

    THE CONSEQUENCE IS A BOOK MUTATION, which is why this is refused rather than skipped. Treating an
    unreadable shape as OPEN books a contra-leg against a position that may have closed, and that
    OPENS a fresh position in a lane holding nothing — worse than the state being repaired. Treating
    it as CLOSED is safer but reports the wrong reason: "this leg is no longer open" is a claim about
    the book, and we do not have one. `contra_close` already refuses this shape by name; the two
    stages must not disagree about it.

    Three states: open / closed / this read cannot describe it.
    """
    class _Mute:
        """What production can hand over when a read is partial — no `is_open` at all."""
        instrument_id = "CGAU.XNYS"
        strategy_id = "MOMENTUM-002"
        signed_qty = -88.0
        avg_px_open = 43.55
        id = "CGAU.XNYS-MOMENTUM-002"
        #: CARRIED, so this fixture isolates the MISSING `is_open`. Without it the double would also
        #: lack `ts_opened` and the test would pass for the wrong reason — an undiscriminating test.
        ts_opened = 1_000

    # FIXTURE PROPERTY FIRST: the double must genuinely lack the attribute, or this proves nothing.
    assert not hasattr(_Mute(), "is_open"), "fixture must not carry is_open"
    assert hasattr(_Mute(), "ts_opened"), "and must carry the OTHER field, or it fails for both"

    ex = _exec(_planned(), [_book()[0], _Mute()])
    assert ex.orders == ()
    assert len(ex.refused) == 1
    assert "cannot describe itself" in ex.refused[0].reason, ex.refused[0].reason


# ==================================================================================================
# BOUNDARIES EVERY FIXTURE ABOVE SITS ON ONE SIDE OF
#
# A mutation pass aimed at INSTANCES rather than the CLASS: three mutants survived not because the
# assertions were weak but because every fixture in this file sits on one side of a boundary — the
# short is always the smaller leg, the short price is always above the long basis, and every pair is
# partial. Each test below puts a fixture on the other side.
# ==================================================================================================
def test_a_SHORT_BIGGER_THAN_ITS_LONG_cannot_reach_the_executor_at_all():
    """Why `min(|long|, |short|)` is never exercised — and why it stays anyway.

    A mutant replacing the `min` with `abs(short_qty)` survived all 32 tests, because every fixture
    had |short| <= |long| so the min collapsed to |short| everywhere the suite looked. The obvious
    reading is a coverage hole. IT IS NOT, AND THE REASON IS WORTH PINNING RATHER THAN REDISCOVERING:

        |short| > |long|  =>  cache net < 0  =>  (net must equal broker)  =>  broker net < 0

    and `plan_contra_closes` refuses a broker-held short outright — a different incident, with a
    different owner. So the state that would distinguish `min` from `abs(short)` cannot be planned,
    and cannot reach execution either: the live legs are fingerprint inputs, so a book that drifted
    into that shape is refused as moved before any leg is booked.

    THE `min` STAYS. It is correct on its own terms and costs nothing, and the guard that makes it
    unreachable lives in ANOTHER FUNCTION for ANOTHER reason — exactly the coupling that rots
    silently. This test is what fails if that refusal is ever relaxed, which is the moment the `min`
    stops being decoration and starts being load-bearing.
    """
    book = _book(long_qty=28.0, short_qty=-54.0)
    assert abs(book[1].signed_qty) > abs(book[0].signed_qty), "fixture must invert the usual order"
    net = sum(p.signed_qty for p in book)
    assert net < 0, "a bigger short means a negative net — this is the arithmetic being pinned"

    plan = plan_contra_closes(book, broker={"CGAU.XNYS": net})
    assert plan.pairs == (), "a broker-held short must not be planned as a mirror pair"
    assert plan.refused and "SHORT" in plan.refused[0].reason.upper(), plan.refused


def test_a_FULL_pair_is_NOT_flagged_partial():
    """`is_partial=True` hardcoded survived the whole suite: no test drove a FULL pair through the
    executor. The dangerous direction (a partial reported as full — a blended basis presented as
    repaired) was pinned; the noisy one was not, and a repair that cries 'blended basis' on every
    clean pair is one that gets ignored on the pair where it is true."""
    book = _book(long_qty=28.0, short_qty=-28.0)
    ex = _exec(_planned(book, broker={"CGAU.XNYS": 0.0}), book, {"CGAU.XNYS": 0.0})
    assert ex.orders[0].is_partial is False
    assert ex.orders[0].legs[0].quantity == 28.0


def test_the_erased_realized_is_NEGATIVE_when_the_short_was_minted_BELOW_the_long_basis():
    """Wrapping `unbooked_realized` in `abs()` survived every test, because every fixture had
    `short_px > long_px` — so a positive-only value pinned sign INVERSION and nothing else.

    A short minted by a sale below the long's basis is ordinary, and its erased realized is
    genuinely negative. Recording it as positive would flip the direction of the permanent
    discrepancy between the lane sums and the broker plane — the one number this repair exists to
    hand forward honestly."""
    book = _book(long_qty=168.0, short_qty=-88.0)
    book[0].avg_px = 43.55      # long basis ABOVE
    book[1].avg_px = 41.10      # short minted BELOW it
    assert book[1].avg_px < book[0].avg_px, "fixture must put the short price below the long basis"

    ex = _exec(_planned(book, broker={"CGAU.XNYS": 80.0}), book)
    assert ex.orders[0].unbooked_realized == pytest.approx((41.10 - 43.55) * 88.0)
    assert ex.orders[0].unbooked_realized < 0


# ==================================================================================================
# WHAT "STILL TRUE NOW" HAS TO MEAN
# ==================================================================================================
def test_a_pair_that_CLOSED_AND_REOPENED_at_the_same_numbers_is_REFUSED():
    """ID STABILITY IS NOT CYCLE IDENTITY under a deterministic id scheme.

    Under NETTING the position id is derived, not allocated — `Cache.add_position` even carries
    "Cleanup for NETTING reopen". So a long that fully closed and RE-OPENED between approval and
    execution, at the same quantity and basis (a same-level limit re-entry is how this happens),
    carries the SAME id, the same qty_before and the same price: an identical fingerprint.

    The repair would then book its SELL leg against a genuinely NEW, REAL position. This is the only
    wrong-aim state that survives every other check, and the fingerprint has no temporal component to
    catch it. `ts_opened` is on the position already and costs nothing.
    """
    book = _book()
    plan = _planned(book)
    reopened = _book()
    reopened[0].ts_opened = book[0].ts_opened + 1     # same id, same numbers, different cycle

    ex = _exec(plan, reopened)
    assert ex.orders == ()
    assert ex.refused


def test_a_BROKER_NET_that_moved_is_REFUSED_and_the_read_must_be_FRESH():
    """The fingerprint's `broker_qty` compared a COPY WITH ITS ORIGINAL, so it could never disagree.

    `_live_pair` set `broker_qty=pair.broker_qty` — carried over from the approved pair rather than
    re-read — which made the one component describing the venue vacuous. Agreement is not connection,
    at exactly the knob that looked most pinned: a test fabricating a wrong value there FAILED,
    proving only that the suite pinned it as a copy.

    WHY IT MATTERS MORE THAN THE OTHER FIELDS. `cache net == broker net` is the premise the entire
    repair rests on. A real partial sale filling at the venue after approval does not touch the cache
    until reconciliation applies it — cache legs unchanged, fingerprint unchanged, legs book — and
    when the sale lands the long lane has given back more than it holds and FLIPS SHORT. The mint,
    through the repair.
    """
    book = _book()
    plan = _planned(book)
    ex = _exec(plan, book, {"CGAU.XNYS": 60.0})   # was 80 at planning
    assert ex.orders == ()
    assert ex.refused
    assert "broker" in ex.refused[0].reason.lower()


def test_a_STOP_RESTED_AFTER_APPROVAL_blocks_the_repair():
    """The #239 guard was PLANNING-TIME ONLY, and the protection reconciler re-arms on a 60s timer.

    So a protective stop rested between approval and execution is unseen — and that stop is sized to
    the PHANTOM quantity, which is the whole hazard: a reduce-only order left working against a
    position this repair shrinks OVERSELLS on trigger, turning a book-only correction into a real
    market event. The vocabulary fix closed the fail-open in that guard; this closes the time gap
    that let the guard be bypassed entirely.
    """
    book = _book()
    plan = _planned(book)
    ex = _exec(plan, book, working_orders={"CGAU.XNYS"})
    assert ex.orders == ()
    assert ex.refused
    assert "resting" in ex.refused[0].reason.lower() or "working" in ex.refused[0].reason.lower()


def test_a_BROKER_READ_THAT_DOES_NOT_MENTION_THE_INSTRUMENT_is_UNKNOWN_not_zero():
    """Absence is a timestamp, not a property. A truncated or partial broker read and a genuinely
    flat account are indistinguishable from here, and reading the first as the second would repair
    against a net nobody confirmed.

    The NaN this produces makes every comparison False, which happens to give the right answer — so
    an explicit unknown-check cannot be shown to change behaviour by mutation. It is kept anyway,
    and stated as an accident rather than a design, because `nan <= 0` being False is the same family
    that silently disarmed a daily-loss halt elsewhere in this codebase. This test pins the OUTCOME,
    which is what actually matters: no orders, and a refusal that names the broker plane.
    """
    ex = _exec(_planned(), _book(), {})          # the read mentions nothing at all
    assert ex.orders == ()
    assert "BROKER" in ex.refused[0].reason.upper()


def test_a_POSITION_WITHOUT_ts_opened_is_REFUSED_not_fingerprinted_as_ZERO():
    """The NETTING id-reuse detector must not be silently disabled by a shape that lacks the field.

    `int(getattr(pos, "ts_opened", 0) or 0)` gives 0 on BOTH reads, so the pair fingerprints `0|0`
    and matches itself — the detector added one commit ago is then vacuous for that shape, and the
    pair books straight through. This is character-for-character the class the broker fix just paid
    for: a value compared against a copy of itself, agreeing by construction.

    Production's Nautilus `Position` carries `ts_opened`, so there is no live exposure today. That is
    exactly why it matters: every double WITHOUT the field is testing a disabled detector, which is
    how the broker copy hid in the first place. Refusing forces the doubles to carry what production
    carries.
    """
    class _NoStamp:
        instrument_id = "CGAU.XNYS"
        strategy_id = "MOMENTUM-002"
        signed_qty = -88.0
        avg_px_open = 43.55
        id = "CGAU.XNYS-MOMENTUM-002"
        is_open = True

    assert not hasattr(_NoStamp(), "ts_opened"), "fixture must lack the stamp"

    ex = _exec(_planned(), [_book()[0], _NoStamp()])
    assert ex.orders == ()
    assert ex.refused and "ts_opened" in ex.refused[0].reason


def test_the_EXECUTOR_guards_the_resting_order_WITH_THE_SAME_PREDICATE_AS_THE_PLANNER():
    """Two stages, one guard, two predicates — the class this repo pins with same-predicate tests.

    The planner learned to block by SYMBOL-PART and to accept a wider `known_instrument_ids`
    universe, because a held sibling listing is legitimate vocabulary that a full-id membership test
    silently passes. The executor kept the old predicate: positions-only ids and full-id membership.

    Both failure directions are real. Without a universe, a sibling-listing stop appearing between
    approval and execution makes `_validated_working` RAISE — fail-closed, but it kills EVERY pair
    including clean ones, violating this module's own per-pair doctrine and crashing on exactly the
    vocabulary the planner now handles as a per-pair refusal. Thread a universe through and the
    full-id membership silently PASSES the sibling instead, which is the #239 guard going quiet again.

    #625 measured 64 of staging's 209 cached instruments carrying two venues, so this is ordinary.
    """
    book = _book()
    plan = _planned(book)

    ex = _exec(plan, book, working_orders={"CGAU.XNAS"},
               known_instrument_ids={"CGAU.XNYS", "CGAU.XNAS"})

    assert ex.orders == (), "a stop on a sibling listing reserves the same shares"
    assert ex.refused and "resting order" in ex.refused[0].reason


def test_the_TWO_STAGES_USE_ONE_PREDICATE_not_two_that_can_drift():
    """Pinned as an identity, not as two behaviours that happen to agree today.

    Leash validation, manager-armed derivation, and percent→bps rounding have each drifted across a
    seam in this repo. A guard whose two ends compute the same question separately is the same shape,
    and this one decides whether a repair runs while a stop sized to the phantom quantity is resting.
    """
    import inspect

    from api import contra_close, contra_execute

    planner = inspect.getsource(contra_close.plan_contra_closes)
    executor = inspect.getsource(contra_execute.prepare_execution)

    for src, name in ((planner, "plan_contra_closes"), (executor, "prepare_execution")):
        assert 'rsplit(".", 1)[0] in working_symbols' in src, (
            f"{name} no longer blocks by symbol-part — the two stages have drifted, and a resting "
            f"order on a sibling venue listing reserves the same shares in both"
        )
        assert "_validated_working" in src, f"{name} bypasses the shared vocabulary validator"


# --------------------------------------------------------------------------------------------
# #771 — stage 2 must read a COMPLETE snapshot's silence the same way stage 1 does
# --------------------------------------------------------------------------------------------
#
# `_live_pair` turns an unmentioned instrument into NaN, and NaN fails `_same_qty`, so a
# fully-offset pair that stage 1 now plans would be refused one stage later for the identical
# reason. The two stages MUST NOT DISAGREE about what they can describe — that rule is already
# written into `plan_contra_closes` for `ts_opened`, and this is the same rule for the broker plane.


def _offset_book(inst="MRVL.XNAS"):
    """The live paper shape: lane long against an EXTERNAL short, equal size, identical basis,
    netting to ZERO — which is why the venue never mentions it."""
    return [
        _Pos(inst, "QC345-003", 14.0, 224.55, f"{inst}-QC345-003"),
        _Pos(inst, "EXTERNAL", -14.0, 224.55, f"{inst}-EXTERNAL"),
    ]


def test_the_offset_fixture_nets_to_zero_so_the_venue_cannot_mention_it():
    """FIXTURE PROPERTY FIRST — without this the completeness flag has nothing to bind on and every
    assertion below would pass whether or not the mechanism existed."""
    assert sum(p.signed_qty for p in _offset_book()) == 0.0


def test_stage_two_still_refuses_an_unmentioned_pair_when_completeness_is_NOT_stated():
    """The guard survives. An unstated read cannot tell flat from unread, and booking against it
    would move a real position."""
    plan = plan_contra_closes(_offset_book(), broker={}, broker_complete=True)
    assert plan.pairs, "fixture produced no pair — the assertion below would be vacuous"
    ex = prepare_execution(plan, _offset_book(), {})
    assert ex.orders == ()
    assert ex.refused


def test_stage_two_accepts_the_offset_pair_when_the_snapshot_is_STATED_COMPLETE():
    """The two stages agree. Stage 1 planned it; stage 2 must be able to execute it."""
    plan = plan_contra_closes(_offset_book(), broker={}, broker_complete=True)
    ex = prepare_execution(plan, _offset_book(), {}, broker_complete=True)
    assert [r.reason for r in ex.refused] == []
    assert len(ex.orders) == 1
    order = ex.orders[0]
    assert order.instrument_id == "MRVL.XNAS"
    assert {leg.side for leg in order.legs} == {"BUY", "SELL"}
    # The aim is READ from the live book, never rebuilt.
    assert {leg.position_id for leg in order.legs} == {
        "MRVL.XNAS-QC345-003", "MRVL.XNAS-EXTERNAL"}
    # Each leg at its own basis — identical here because the mint copied the price, which is
    # precisely what makes this pair identifiable as minted rather than traded.
    assert {leg.price for leg in order.legs} == {224.55}


def test_completeness_at_stage_two_does_not_mask_a_pair_that_actually_MOVED():
    """Completeness fills silence only. A venue that now reports a real position still refuses the
    pair, so a partial sale between approval and execution cannot be booked over."""
    plan = plan_contra_closes(_offset_book(), broker={}, broker_complete=True)
    ex = prepare_execution(plan, _offset_book(), {"MRVL.XNAS": 9.0}, broker_complete=True)
    assert ex.orders == ()
    assert ex.refused


# --------------------------------------------------------------------------------------------
# #779 — THE SEAM. Without these, every planner test stays green while NOTHING BOOKS.
# --------------------------------------------------------------------------------------------


def _gmab():
    """The live GMAB shape: 118 long against 59 short, and a venue holding none of it."""
    return [
        _Pos("GMAB.XNAS", "BCTROT-004", 59.0, 33.41, "GMAB.XNAS-BCTROT-004"),
        _Pos("GMAB.XNAS", "MOMENTUM-002", 59.0, 33.94, "GMAB.XNAS-MOMENTUM-002"),
        _Pos("GMAB.XNAS", "EXTERNAL", -59.0, 33.68, "GMAB.XNAS-EXTERNAL"),
    ]


def _gmab_plan(book=None):
    plan = plan_contra_closes(book or _gmab(), broker={}, broker_complete=True)
    assert plan.evictions, "fixture produced no eviction — every assertion below would be vacuous"
    return plan


def test_an_eviction_REACHES_execution_as_one_order_carrying_every_leg():
    """The capability nothing calls is indistinguishable from one that does not work. Killed by
    deleting the eviction loop in `prepare_execution`."""
    plan = _gmab_plan()
    ex = prepare_execution(plan, _gmab(), {}, broker_complete=True)
    assert [r.reason for r in ex.refused] == []
    assert len(ex.orders) == 1
    order = ex.orders[0]
    assert order.fingerprint == plan.evictions[0].fingerprint
    assert {(l.strategy_id, l.quantity, l.side) for l in order.legs} == {
        ("BCTROT-004", 59.0, "SELL"),
        ("MOMENTUM-002", 59.0, "SELL"),
        ("EXTERNAL", 59.0, "BUY"),
    }
    # An eviction leaves no survivor, so nothing carries a blended basis onward.
    assert order.is_partial is False


def test_an_eviction_whose_leg_VANISHED_is_refused_WHOLE_never_trimmed():
    """Booking the rest would land the cache on a NEW wrong net — neither the approved book nor the
    venue's zero. Killed by trimming to the surviving legs."""
    plan = _gmab_plan()
    live = [p for p in _gmab() if p.strategy_id != "MOMENTUM-002"]
    ex = prepare_execution(plan, live, {}, broker_complete=True)
    assert ex.orders == ()
    assert any("refused whole, never trimmed" in r.reason for r in ex.refused)


def test_an_eviction_that_gained_an_EXTRA_leg_is_also_refused():
    """An extra leg does not narrow an eviction, it FALSIFIES ITS PREMISE — the claim is to flatten
    the whole instrument. Killed by a lane-set check that only tests `planned <= live`."""
    plan = _gmab_plan()
    live = _gmab() + [_Pos("GMAB.XNAS", "QC345-003", 5.0, 33.5, "GMAB.XNAS-QC345-003")]
    ex = prepare_execution(plan, live, {}, broker_complete=True)
    assert ex.orders == ()
    assert any("refused whole, never trimmed" in r.reason for r in ex.refused)


def test_the_venue_premise_is_RE_READ_at_execution_not_carried_over():
    """A venue that no longer states zero means a residual must be awarded — the guess this refuses.
    Killed by comparing `ev.broker_qty` with itself instead of re-reading, which is the vacuous
    broker-copy defect `_live_pair` already had to fix."""
    plan = _gmab_plan()
    ex = prepare_execution(plan, _gmab(), {"GMAB.XNAS": 7.0}, broker_complete=True)
    assert ex.orders == ()
    assert any("no longer states zero" in r.reason for r in ex.refused)


def test_an_eviction_is_refused_when_the_read_was_never_vouched_complete():
    """Silence is not zero at execution either. Killed by defaulting the absent broker entry to 0.0
    regardless of `broker_complete`."""
    plan = _gmab_plan()
    ex = prepare_execution(plan, _gmab(), {})
    assert ex.orders == ()
    assert any("no longer states zero" in r.reason for r in ex.refused)


def test_a_resting_order_appearing_after_approval_refuses_the_eviction():
    """#239: a reduce-only order sized to the PHANTOM quantity oversells on trigger once the book
    beneath it is gone."""
    plan = _gmab_plan()
    ex = prepare_execution(plan, _gmab(), {}, broker_complete=True,
                           working_orders=["GMAB.XNAS"], known_instrument_ids=["GMAB.XNAS"])
    assert ex.orders == ()
    assert any("resting order" in r.reason for r in ex.refused)
