"""Planning the ownership repair — and everything it must refuse to plan (#437 piece 1).

WHAT IS BEING REPAIRED. A closing SELL booked to the wrong owner mints, under NETTING, an offsetting
SHORT in a sibling lane while the real owner's LONG is left stranded. The netted book still equals
the broker's exactly, which is why all three reconcilers read green for nine days. $18,217.94 across
eight pairs.

THE REPAIR IS BOOK-ONLY AND IT REALIZES NOTHING. Each leg fills at ITS OWN position's `avg_px_open`,
so neither lane books P&L. Filling at mark would invent realized P&L for an economic event that
happened days ago at a different price.

WHAT IT DOES NOT FIX, STATED SO NOBODY ASSUMES OTHERWISE: the realized P&L of the original
mis-attributed sale stays with the lane that wrongly booked it. That money moved on a real fill at a
real price; no book entry can re-attribute it without inventing a trade. This repairs the OPEN book.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from api.contra_close import ContraPlan, plan_contra_closes, plan_operator_pair


@dataclass
class _Pos:
    """Shaped from a Nautilus Position: the fields the planner reads, and `is_open`, which is the
    one that keeps a CLOSED position out of the plan."""

    instrument_id: str
    strategy_id: str
    signed_qty: float
    avg_px: float
    pos_id: str
    is_open: bool = True
    #: PRODUCTION CARRIES THIS and both stages read it — the planner fingerprints it and the executor
    #: refuses a shape without it. A double lacking it was testing a DISABLED id-reuse detector,
    #: which is the shape that let the broker_qty copy hide.
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


def _mirror(inst="CGAU.XNYS", long_lane="BCTROT-004", short_lane="MOMENTUM-002",
            long_qty=168.0, short_qty=-88.0):
    return [
        _Pos(inst, long_lane, long_qty, 41.10, f"{inst}-{long_lane}"),
        _Pos(inst, short_lane, short_qty, 43.55, f"{inst}-{short_lane}"),
    ]


# ==================================================================================================
# WHAT IT PLANS
# ==================================================================================================
def test_a_mirror_pair_is_planned_for_the_SMALLER_of_the_two_legs():
    """The short is entirely phantom, so it closes completely; the stranded long gives back exactly
    what the short claims. `min(|qty|)` is the only quantity that leaves the net unchanged."""
    plan = plan_contra_closes(_mirror(), broker={"CGAU.XNYS": 80.0})
    assert len(plan.pairs) == 1
    p = plan.pairs[0]
    assert p.quantity == 88.0
    assert (p.long_strategy, p.short_strategy) == ("BCTROT-004", "MOMENTUM-002")


def test_each_leg_is_priced_at_ITS_OWN_avg_px_open_so_neither_lane_realizes_anything():
    """CARRY_OVER, and the reason it is per-leg rather than one price: the two positions have
    different bases. One price for both would realize P&L on whichever leg it did not match — a
    fictional gain or loss on an event that already happened at a different price days ago."""
    plan = plan_contra_closes(_mirror(), broker={"CGAU.XNYS": 80.0})
    p = plan.pairs[0]
    # FIXTURE PROPERTY FIRST: the two bases must actually differ, or a single-price implementation
    # passes this test while being wrong.
    assert p.long_px != p.short_px, "the fixture must have two different bases"
    assert (p.long_px, p.short_px) == (41.10, 43.55)


def test_the_plan_carries_the_REAL_position_ids_read_from_the_cache():
    """THE SHARPEST TRAP. `_apply_leg` hardcodes `PositionId(f"{instrument}-{strategy}")`, and a fill
    aimed at an id that is not the real one OPENS A NEW POSITION instead of closing the short —
    re-minting the exact defect being repaired.

    A cache read confirmed all sixteen live legs happen to match that shape, but for ONE of them the
    embedded position_id was actually inspected. Being right by coincidence is not being right, so
    the plan carries the id it READ and the executor uses that."""
    positions = _mirror()
    # BOTH ids must differ from the reconstructed shape. The first version only changed the SHORT
    # leg's, and the mutant that rebuilds `long_position_id` from `f"{instrument}-{strategy}"`
    # SURVIVED — the long's real id happened to equal the reconstruction, so the test could not tell
    # a read from a guess on that side. A fixture whose value the wrong code can also produce proves
    # nothing about which one produced it.
    positions[0].pos_id = "CGAU.XNYS-BCTROT-004-ADOPTED-1a90"
    positions[1].pos_id = "CGAU.XNYS-MOMENTUM-002-RECONCILED-7f31"
    plan = plan_contra_closes(positions, broker={"CGAU.XNYS": 80.0})
    p = plan.pairs[0]

    # FIXTURE PROPERTY FIRST: neither id may be reconstructible, or the assertions below pass for a
    # reconstruction.
    for pos in positions:
        assert pos.pos_id != f"{pos.instrument_id}-{pos.strategy_id}"
    assert p.short_position_id == "CGAU.XNYS-MOMENTUM-002-RECONCILED-7f31"
    assert p.long_position_id == "CGAU.XNYS-BCTROT-004-ADOPTED-1a90"


# ==================================================================================================
# WHAT IT REFUSES
# ==================================================================================================
def test_a_CLOSED_position_is_never_planned_against():
    """THE TRAP A KEY-PATTERN SCAN WALKS INTO. The durable cache retains CLOSED positions under the
    same id shape — `WHD.XNYS-MANUAL-001` is a flatten that already ran. Selecting on key existence
    rather than `is_open` would book contra-legs against them and OPEN BRAND-NEW POSITIONS in lanes
    that currently hold nothing: worse than the defect being repaired.

    Measured on the live cache before this was written, not imagined."""
    positions = _mirror() + [_Pos("CGAU.XNYS", "MANUAL-001", 0.0, 42.0,
                                  "CGAU.XNYS-MANUAL-001", is_open=False)]
    plan = plan_contra_closes(positions, broker={"CGAU.XNYS": 80.0})
    assert len(plan.pairs) == 1
    assert "MANUAL-001" not in (plan.pairs[0].long_strategy, plan.pairs[0].short_strategy)


def test_an_instrument_where_the_CACHE_NET_DISAGREES_with_the_broker_is_REFUSED():
    """The premise of the whole repair is that the account is FLAT and only the split is wrong. Where
    the net itself disagrees, something else is going on and a contra-close would move a real
    position. That is a different problem and this must not touch it."""
    plan = plan_contra_closes(_mirror(), broker={"CGAU.XNYS": 999.0})
    assert plan.pairs == ()
    assert any("does not agree with the broker" in r.reason for r in plan.refused)


def test_an_instrument_the_BROKER_NEVER_MENTIONED_is_REFUSED_not_assumed_flat():
    """Absence is not zero. A symbol missing from the broker payload might be genuinely flat or might
    be a truncated read, and the two are indistinguishable from here. Three states."""
    plan = plan_contra_closes(_mirror(), broker={})
    assert plan.pairs == ()
    assert any("did not mention" in r.reason for r in plan.refused)


def test_an_instrument_with_RESTING_ORDERS_is_REFUSED():
    """#239 rests protective stops sized to the phantom quantities. A reduce-only order against a
    position about to shrink will OVERSELL when it triggers — turning a book repair into a real
    market event."""
    plan = plan_contra_closes(_mirror(), broker={"CGAU.XNYS": 80.0},
                              working_orders={"CGAU.XNYS"})
    assert plan.pairs == ()
    assert any("resting order" in r.reason for r in plan.refused)


def test_a_lane_holding_a_SHORT_with_no_offsetting_LONG_is_REFUSED():
    """A short with nothing to net against is not a mirror — the long may sit in a lane this read
    cannot see, or the short may be real. Booking a contra-leg alone would change the net."""
    plan = plan_contra_closes([_Pos("TOST.XNYS", "EXTERNAL", -74.0, 30.0, "TOST.XNYS-EXTERNAL")],
                              broker={"TOST.XNYS": 0.0})
    assert plan.pairs == ()
    assert any("no offsetting long" in r.reason for r in plan.refused)


def test_THREE_legs_on_one_instrument_are_REFUSED_rather_than_paired_arbitrarily():
    """Two longs and a short, or two shorts and a long, has no single correct pairing from this
    plane — choosing one would be a guess dressed as a repair."""
    positions = _mirror() + [_Pos("CGAU.XNYS", "QC345-003", 20.0, 40.0, "CGAU.XNYS-QC345-003")]
    plan = plan_contra_closes(positions, broker={"CGAU.XNYS": 100.0})
    assert plan.pairs == ()
    assert any("more than two" in r.reason for r in plan.refused)


# ==================================================================================================
# THE PLAN IS A FIXED LIST, NOT A PREDICATE
# ==================================================================================================
def test_the_plan_is_EMPTY_when_the_book_is_clean_and_says_so_rather_than_erroring():
    """A clean book is the expected steady state once this has run. It must be a quiet no-op, not an
    exception, or nobody will run it again to check."""
    plan = plan_contra_closes([_Pos("AEM.XNYS", "MOMENTUM-002", 10.0, 100.0, "AEM.XNYS-MOMENTUM-002")],
                              broker={"AEM.XNYS": 10.0})
    assert plan.pairs == () and plan.refused == ()
    assert "nothing to repair" in plan.summary


def test_the_plan_carries_a_FRESHNESS_STAMP_per_pair_so_execution_can_refuse_a_moved_pair():
    """Approval is of a FIXED LIST, never a predicate re-evaluated at execution time. A pair whose
    quantities moved between approval and execution is REFUSED, not silently resized to the new
    numbers — resizing would execute something nobody approved."""
    plan = plan_contra_closes(_mirror(), broker={"CGAU.XNYS": 80.0})
    p = plan.pairs[0]
    assert p.fingerprint, "each pair must carry an identity execution can re-derive"
    moved = _mirror(long_qty=170.0)
    assert plan_contra_closes(moved, broker={"CGAU.XNYS": 82.0}).pairs[0].fingerprint != p.fingerprint


# ==================================================================================================
# ABSENCE MUST NOT READ AS PERMISSION — in the direction that MUTATES A BOOK
# ==================================================================================================
def test_a_position_that_cannot_say_whether_it_is_OPEN_is_REFUSED_not_assumed_open():
    """`getattr(p, "is_open", True)` made a shape lacking the field count as OPEN — eligible for a
    plan that books fills against it. Absence reading as permission, in the one direction where the
    consequence is a book mutation rather than a missing row.

    A Nautilus Position always has `is_open`, which is exactly the reasoning that makes this look
    safe to default. The durable cache also always had real position ids matching the reconstructed
    shape, and that assumption is the one this whole module refuses to make."""
    class _Shapeless:
        instrument_id = "CGAU.XNYS"
        strategy_id = "MOMENTUM-002"
        signed_qty = -88.0
        avg_px_open = 43.55
        id = "CGAU.XNYS-MOMENTUM-002"
        # deliberately NO is_open

    positions = [_Pos("CGAU.XNYS", "BCTROT-004", 168.0, 41.10, "CGAU.XNYS-BCTROT-004"), _Shapeless()]
    # FIXTURE PROPERTY FIRST: the double must really lack the attribute, or this tests nothing.
    assert not hasattr(_Shapeless(), "is_open")

    plan = plan_contra_closes(positions, broker={"CGAU.XNYS": 80.0})
    assert plan.pairs == ()
    assert any("cannot say whether it is open" in r.reason for r in plan.refused), plan.refused


def test_a_broker_position_that_is_ITSELF_SHORT_is_REFUSED_and_named():
    """A genuinely short broker position in a long-only book is a crisis of its own, and the mirror
    arithmetic quietly accommodates it: long +20 / short -100 against a broker at -80 nets correctly,
    so a 20-share pair plans and a REAL net short is left standing, unremarked, looking repaired.

    The repair's premise is that the account is flat or long and only the split is wrong. Where that
    premise fails the answer is to say so, not to plan around it."""
    positions = [_Pos("CGAU.XNYS", "BCTROT-004", 20.0, 41.10, "CGAU.XNYS-BCTROT-004"),
                 _Pos("CGAU.XNYS", "MOMENTUM-002", -100.0, 43.55, "CGAU.XNYS-MOMENTUM-002")]
    # FIXTURE PROPERTY FIRST: the nets must AGREE, so the only thing refusing this is the sign.
    assert 20.0 + -100.0 == -80.0
    plan = plan_contra_closes(positions, broker={"CGAU.XNYS": -80.0})
    assert plan.pairs == (), "a real net short must not be planned around"
    assert any("broker itself holds a SHORT" in r.reason for r in plan.refused), plan.refused


def test_the_fingerprint_covers_the_POSITION_IDS_not_only_the_numbers():
    """Two books with identical quantities and bases under DIFFERENT cache position ids re-derived
    the same fingerprint. Execution aims fills at those ids, and an id that changed between approval
    and execution is the difference between closing a short and OPENING A NEW POSITION — the exact
    defect being repaired. The identity must cover everything execution acts on."""
    a = plan_contra_closes(_mirror(), broker={"CGAU.XNYS": 80.0}).pairs[0]
    moved = _mirror()
    moved[1].pos_id = "CGAU.XNYS-MOMENTUM-002-REBOOKED-4c12"
    b = plan_contra_closes(moved, broker={"CGAU.XNYS": 80.0}).pairs[0]
    # FIXTURE PROPERTY FIRST: everything EXCEPT the id is identical, so only the id can move it.
    assert (a.quantity, a.long_px, a.short_px, a.broker_qty) == (b.quantity, b.long_px, b.short_px, b.broker_qty)
    assert a.fingerprint != b.fingerprint


# ==================================================================================================
# WHAT THE REPAIR ERASES, RECORDED — because two derivations will disagree forever otherwise
# ==================================================================================================
def test_the_repair_records_the_realized_it_ERASES_from_the_lane_ledgers():
    """MY PREMISE WAS WRONG AND THE CORRECTION INVERTS THE ANSWER.

    I documented this as "the realized P&L stays with the lane that wrongly booked it". It does not.
    The wrong-lane SELL fired FROM FLAT, so under NETTING it OPENED the short — and an opening fill
    has no prior lots to match against, so it realizes NOTHING. That sale's realized was never booked
    to ANY lane; it lives only in account cash.

    The repair then closes both legs at their own `avg_px_open`, realizing zero on each. So
    `(short_px - long_px) x qty` — the money that actually moved when the owner's shares left the
    venue — is not mis-attributed. It is ERASED from the lane ledgers, permanently.

    That makes documenting insufficient by this repo's own standard: sum-of-lane-realized against
    `realized_broker`'s plane would disagree forever by exactly this amount, with the explanation
    thrown away. A two-derivations-disagree condition whose reason was discarded is the shape these
    tickets keep being about.

    RECORDED AS A LEDGER DELTA, NOT ECONOMIC TRUTH. For the EXTERNAL shorts the synthetic flatting
    fill's price may differ from the true sale price; the ledger number is still the right one to
    pin, because it is the one the two planes will disagree by.
    """
    plan = plan_contra_closes(_mirror(), broker={"CGAU.XNYS": 80.0})
    p = plan.pairs[0]
    # 88 shares, short opened at 43.55 against a long basis of 41.10.
    assert p.unbooked_realized == pytest.approx((43.55 - 41.10) * 88.0)
    assert plan.unbooked_realized_total == pytest.approx(p.unbooked_realized)
    assert "never booked to any lane" in plan.summary


def test_a_PARTIAL_pair_is_flagged_because_the_surviving_long_keeps_a_WRONG_BASIS():
    """RAISED UNPROMPTED BY REVIEW, and it is the half I would not have found.

    BCTROT's CGAU 168 is a BLEND of the phantom 88 and a real re-entry of 80. Contra-closing 88 at
    the blend leaves the surviving 80 still at the blend: quantity becomes broker-true, basis does
    not, and the survivor's unrealized is misstated from day one.

    No zero-realizing close can fix a basis — that is arithmetic, not an omission. So the pair is
    FLAGGED and the residual recorded, rather than the repair being reported as complete."""
    plan = plan_contra_closes(_mirror(), broker={"CGAU.XNYS": 80.0})
    p = plan.pairs[0]
    assert p.is_partial is True
    assert p.surviving_long_qty == 80.0
    assert "basis" in plan.summary


def test_a_FULL_phantom_pair_has_no_residual_and_is_not_flagged():
    """WHD: long 28 against short -28, broker 0. The long is entirely phantom, so it closes
    completely and no survivor carries a blended basis. Flagging these too would make the flag
    meaningless — five of the eight live pairs are this shape."""
    positions = _mirror(inst="WHD.XNYS", long_qty=28.0, short_qty=-28.0)
    plan = plan_contra_closes(positions, broker={"WHD.XNYS": 0.0})
    p = plan.pairs[0]
    # FIXTURE PROPERTY FIRST: this really is the full-phantom shape, or the assertion is vacuous.
    assert p.quantity == abs(p.long_qty_before) == abs(p.short_qty_before)
    assert p.is_partial is False and p.surviving_long_qty == 0.0


# ==================================================================================================
# ONE VOCABULARY FOR INSTRUMENTS (#744)
# ==================================================================================================
def test_working_orders_IN_THE_WRONG_VOCABULARY_is_REFUSED_not_silently_ignored():
    """The guard that protects a repair from a resting stop cannot fail OPEN.

    `working_orders` is caller-supplied and had no format contract. The positions plane speaks
    instrument ids (`CGAU.XNYS`); a caller reaching for the obvious source hands over bare symbols
    (`CGAU`), and `if instrument in working` is then False for every instrument that has ever
    existed. The guard does not misfire — it NEVER fires.

    WHY THAT SPECIFIC GUARD MATTERS. #239 rests protective stops sized to the PHANTOM quantity. A
    reduce-only order left working against a position this repair is about to shrink OVERSELLS when
    it triggers, turning a book-only correction into a real market event on the live account. That
    is the one failure mode that converts this repair from safe to expensive.

    THE TELL IS THE ASYMMETRY. The `broker` dict is keyed the same way and fails CLOSED on the same
    mismatch — an instrument missing from it is refused with "the broker payload did not mention
    this instrument". Two caller-supplied mappings, one vocabulary, opposite failure directions, and
    only one of them says anything. A silent fail-open beside a loud fail-closed is not a style
    inconsistency; it is the more dangerous half being the quiet one.

    THREE STATES, NOT TWO: this instrument has a working order / it does not / the caller handed me a
    vocabulary I cannot compare, which is neither.
    """
    positions = _mirror()
    # FIXTURE PROPERTY FIRST — the fixture must actually present the mismatch, or the assertion
    # below passes against a rule that does nothing.
    assert all("." in p.instrument_id for p in positions), "positions must speak instrument ids"
    assert "CGAU" not in {p.instrument_id for p in positions}, "the bare symbol must NOT match"

    with pytest.raises(ValueError, match="vocabulary|instrument id"):
        plan_contra_closes(
            positions,
            broker={"CGAU.XNYS": 80.0},
            working_orders={"CGAU"},          # bare symbol — the silent fail-open
        )


def test_working_orders_in_the_RIGHT_vocabulary_still_blocks_the_repair():
    """The refusal must survive the vocabulary check — this is the behaviour being protected, and a
    validator that made the guard stop working would be a worse defect than the one it fixes."""
    plan = plan_contra_closes(
        _mirror(),
        broker={"CGAU.XNYS": 80.0},
        working_orders={"CGAU.XNYS"},
    )
    assert plan.pairs == ()
    assert [r.instrument_id for r in plan.refused] == ["CGAU.XNYS"]
    assert "resting order" in plan.refused[0].reason


def test_an_EMPTY_working_orders_set_is_not_a_vocabulary_error():
    """Nothing resting is the common case and must stay cheap. An empty set carries no vocabulary to
    disagree with — refusing it would make the validator fire on every clean book."""
    plan = plan_contra_closes(_mirror(), broker={"CGAU.XNYS": 80.0}, working_orders=set())
    assert len(plan.pairs) == 1


def test_working_orders_naming_an_instrument_NOT_IN_THE_BOOK_is_fine():
    """A resting order on a name this repair does not touch is ordinary, not a mismatch. The check is
    on the VOCABULARY — does this look like an instrument id — not on membership of the book, which
    would refuse a perfectly good caller for mentioning an unrelated instrument."""
    plan = plan_contra_closes(
        _mirror(), broker={"CGAU.XNYS": 80.0}, working_orders={"NVDA.XNAS"},
    )
    assert len(plan.pairs) == 1


def test_a_LOWERCASE_instrument_id_is_the_SAME_fail_open_and_is_refused():
    """`InstrumentId.from_str` does not validate case, so 'cgau.xnys' parses — and the guard's
    membership test is case-sensitive, so it matches nothing. Identical silent fail-open to the bare
    symbol, and the first version of this validator did not cover it."""
    with pytest.raises(ValueError, match="vocabulary|instrument id|case"):
        plan_contra_closes(_mirror(), broker={"CGAU.XNYS": 80.0},
                           working_orders={"cgau.xnys"})


def test_a_BARE_SYMBOL_THAT_PARSES_AS_AN_ID_is_still_refused_using_the_BOOK_S_OWN_SYMBOLS():
    """The residual I dismissed too broadly, closed with what was already in hand.

    `from_str` accepts 'BRK.B' as symbol BRK on venue B, so a bare symbol containing a dot parses and
    slips through a parser-only check. I wrote that off on the grounds that distinguishing it needs
    the VENUE set, which is not knowable from an unrelated resting order.

    That framing was wrong. It needs the SYMBOL set, and the symbol set is sitting in the `positions`
    argument. An item that equals the symbol-part of a held instrument id while equalling no held id
    is a CERTAIN vocabulary error — it can only be the caller having stripped the venue — and the
    check still refuses nothing legitimate, because an unrelated instrument shares neither.

    The live paper book holds BOTH `BRK.B` and `BRKB`, so this is not a theoretical shape.
    """
    book = _mirror(inst="BRK.B.XNYS")
    # FIXTURE PROPERTY: the bare form must genuinely parse, or this tests the parser check instead.
    from nautilus_trader.model.identifiers import InstrumentId
    InstrumentId.from_str("BRK.B")          # does not raise — that is the whole problem

    with pytest.raises(ValueError, match="vocabulary|instrument id|venue"):
        plan_contra_closes(book, broker={"BRK.B.XNYS": 80.0}, working_orders={"BRK.B"})


def test_an_UNRELATED_instrument_is_still_accepted_after_the_symbol_check():
    """The symbol check must not become a membership check. A resting order on a name this repair
    does not touch is ordinary — refusing it would make the guard fire on healthy callers, which is
    how a guard gets switched off."""
    plan = plan_contra_closes(_mirror(), broker={"CGAU.XNYS": 80.0},
                              working_orders={"NVDA.XNAS"})
    assert len(plan.pairs) == 1


def test_a_RESTING_ORDER_ON_A_SIBLING_VENUE_of_a_held_symbol_is_refused():
    """The venue-variant hole, and #625 makes it concrete rather than theoretical.

    `CGAU.XNAS` against a book holding `CGAU.XNYS` is well-formed, upper-case, not a held id and not
    a bare symbol — so it passed every check and the pair was PLANNED with that order resting. The
    #239 guard silently never fired, which is the exact failure the validator exists to end, wearing
    a third disguise.

    IT IS THE SHAPE THIS FEED PRODUCES. #625 measured 64 of staging's 209 cached instruments carrying
    TWO venues, because SMART routing returns multiple listings. A resting-order row surfaced under
    the sibling venue id, against positions keyed by the primary exchange, is ordinary here.

    BLOCK BY SYMBOL-PART, NOT BY FULL ID. On this book the same ticker is the same security, so an
    order resting on any venue variant reserves the same shares. Failing toward refusal is this
    guard's correct direction: the cost of refusing wrongly is a repair postponed, and the cost of
    accepting wrongly is a stop sized to the phantom quantity overselling on trigger.
    """
    book = _mirror(inst="CGAU.XNYS")
    with pytest.raises(ValueError, match="vocabulary|instrument id|venue"):
        plan_contra_closes(book, broker={"CGAU.XNYS": 80.0},
                           working_orders={"CGAU.XNAS"})


def test_a_NaN_BROKER_QUANTITY_is_refused_rather_than_planned():
    """A NaN broker value planned a pair: `nan < 0` is False at the broker-short check and
    `abs(net - nan) > tol` is False at the agreement check, so it slipped both. The executor refuses
    it later via `_same_qty` — but the approved list an OPERATOR SIGNS OFF would carry a NaN pair,
    and an approval step that shows unusable numbers is worse than one that shows fewer.

    Same family as `nan <= 0` silently disarming a daily-loss halt: every comparison written for
    numbers is False against NaN, so a guard built from comparisons is not a guard.
    """
    plan = plan_contra_closes(_mirror(), broker={"CGAU.XNYS": float("nan")})
    assert plan.pairs == ()
    assert plan.refused and "not a number" in plan.refused[0].reason.lower()


def test_a_NaN_QUANTITY_on_a_leg_is_refused_BY_NAME_not_silently_dropped():
    """A NaN short quantity fell out of BOTH sign buckets, so the instrument read as clean and was
    skipped without a word. Fail-inert, but the planner already refuses unreadable OPENNESS by name —
    refusing unreadable QUANTITY silently is the same read failing loudly in one place and quietly in
    the other, and the quiet one is where a naked position hides."""
    book = _mirror()
    book[1].signed_qty = float("nan")
    plan = plan_contra_closes(book, broker={"CGAU.XNYS": 80.0})
    assert plan.pairs == ()
    assert plan.refused and "not a number" in plan.refused[0].reason.lower()


def test_a_DUAL_LISTED_symbol_blocks_on_EITHER_venue_at_the_GUARD_not_just_the_validator():
    """The hole MOVED rather than closing, and the validator cannot close it.

    With BOTH `CGAU.XNYS` and `CGAU.XNAS` held, an order resting under either is legitimate
    vocabulary — `item in ids` short-circuits, correctly, so no ValueError is raised. But the GUARD's
    membership test is still full-id: positions mirrored on XNYS against a resting order under XNAS
    means `"CGAU.XNYS" in {"CGAU.XNAS"}` is False, the pair is PLANNED, and the #239 guard is silent
    again. Same shares reserved, no vocabulary error for the validator to catch.

    #625 measured 64 of staging's 209 cached instruments carrying two venues, so this is the ordinary
    shape rather than a corner. The comparison has to go by SYMBOL-PART: on this book the same ticker
    is the same security, and an order on any listing of it reserves the same shares.
    """
    book = _mirror(inst="CGAU.XNYS")
    plan = plan_contra_closes(
        book,
        broker={"CGAU.XNYS": 80.0},
        # Legitimate vocabulary: a sibling listing that IS held, so the validator accepts it.
        working_orders={"CGAU.XNAS"},
        known_instrument_ids={"CGAU.XNYS", "CGAU.XNAS"},
    )
    assert plan.pairs == (), "a resting order on a sibling listing reserves the same shares"
    assert plan.refused and "resting order" in plan.refused[0].reason


def test_the_PLANNER_refuses_a_position_without_ts_opened_just_as_the_EXECUTOR_does():
    """The two stages must not disagree about what they can describe.

    `prepare_execution` refuses a shape lacking `ts_opened`, because defaulting it to 0 on both reads
    makes the NETTING id-reuse check fingerprint against a copy of itself. The planner was still
    `getattr(..., 0)`-defaulting it, so it would happily put such a pair into an approval list an
    OPERATOR SIGNS OFF and the executor is then guaranteed to bounce.

    Fail-closed in outcome, and wrong in vocabulary — which is the half this repo keeps paying for: a
    guard whose two ends describe the same state differently is one nobody can reason about. Same
    predicate, both stages.
    """
    class _NoStamp:
        instrument_id = "CGAU.XNYS"
        strategy_id = "MOMENTUM-002"
        signed_qty = -88.0
        avg_px_open = 43.55
        id = "CGAU.XNYS-MOMENTUM-002"
        is_open = True

    assert not hasattr(_NoStamp(), "ts_opened"), "fixture must lack the stamp"
    assert hasattr(_NoStamp(), "is_open"), "and must carry the OTHER field, or it fails for both"

    plan = plan_contra_closes([_mirror()[0], _NoStamp()], broker={"CGAU.XNYS": 80.0})
    assert plan.pairs == ()
    assert plan.refused and "when its current cycle opened" in plan.refused[0].reason


# --------------------------------------------------------------------------------------------
# #771 — a COMPLETE broker snapshot's silence is "zero", not "never told us"
# --------------------------------------------------------------------------------------------
#
# MEASURED ON PAPER 2026-09-01: four clean minted pairs (MRVL, RBRK, TOST, VEEV) — lane long and
# EXTERNAL short, equal quantity, identical basis — every one refused, because a fully-offset pair
# nets to ZERO and Alpaca's /v2/positions returns only NON-ZERO positions. The repair's own subject
# is invisible to the repair: netting to zero at the broker IS the signature of the thing being
# repaired, and it is the single condition under which the guard declines.


def _minted_pair(instrument="MRVL.XNAS"):
    """The exact live shape: QC345 long 14 @ 224.55 against an EXTERNAL short of the same size and
    the same basis. Same basis is not decoration — the synthetic fill was priced off the phantom it
    was invented to offset, and that is what identifies it as minted rather than traded."""
    return [
        _Pos(instrument, "QC345-003", 14.0, 224.55, f"{instrument}-QC345-003"),
        _Pos(instrument, "EXTERNAL", -14.0, 224.55, f"{instrument}-EXTERNAL"),
    ]


def test_the_fixture_really_is_invisible_to_the_broker():
    """FIXTURE PROPERTY FIRST. If the pair did not net to zero the guard could not bind either way
    and everything below would pass without testing anything — the vacuous-fixture shape that let
    kumo-strategies' truncation test pass twice with the bug reintroduced."""
    legs = _minted_pair()
    assert sum(p.signed_qty for p in legs) == 0.0
    # ...and therefore a venue that reports only non-zero positions cannot mention it.
    broker_payload = {i: q for i, q in {"MRVL.XNAS": 0.0}.items() if q != 0.0}
    assert "MRVL.XNAS" not in broker_payload


def test_an_incomplete_read_still_refuses_because_absence_is_not_zero():
    """THE GUARD MUST SURVIVE. This is the default and the reason the rule exists: a truncated or
    failed read genuinely cannot distinguish flat from unread, and booking against it moves a real
    position. Nothing below may weaken this."""
    plan = plan_contra_closes(_minted_pair(), broker={})
    assert plan.pairs == ()
    assert any("did not mention" in r.reason for r in plan.refused)


def test_a_complete_snapshot_lets_the_fully_offset_pair_be_planned():
    """The fix. A caller that STATES its payload is a complete snapshot has genuinely been told
    zero, and the pair becomes repairable."""
    plan = plan_contra_closes(_minted_pair(), broker={}, broker_complete=True)
    assert [r.reason for r in plan.refused] == []
    assert len(plan.pairs) == 1
    pair = plan.pairs[0]
    assert pair.instrument_id == "MRVL.XNAS"
    assert pair.long_strategy == "QC345-003"
    assert pair.short_strategy == "EXTERNAL"
    assert pair.quantity == 14.0
    # The broker quantity it was planned against must be the ZERO it was told, not a copy of the
    # cache net — those agree here, which is exactly when a severed wire is invisible.
    assert pair.broker_qty == 0.0


def test_completeness_does_not_invent_a_position_the_venue_contradicts():
    """Completeness only fills SILENCE. An instrument the venue DID mention keeps the venue's number,
    so a real disagreement is still caught rather than overwritten with zero."""
    legs = [
        _Pos("MRVL.XNAS", "QC345-003", 14.0, 224.55, "MRVL.XNAS-QC345-003"),
        _Pos("MRVL.XNAS", "EXTERNAL", -4.0, 224.55, "MRVL.XNAS-EXTERNAL"),
    ]
    plan = plan_contra_closes(legs, broker={"MRVL.XNAS": 99.0}, broker_complete=True)
    assert plan.pairs == ()
    assert any("does not agree with the broker" in r.reason for r in plan.refused)


def test_completeness_does_not_reopen_the_broker_short_refusal():
    """A venue that reports a genuine SHORT is still its own incident, complete snapshot or not."""
    plan = plan_contra_closes(_minted_pair(), broker={"MRVL.XNAS": -5.0}, broker_complete=True)
    assert plan.pairs == ()
    assert any("holds a SHORT" in r.reason for r in plan.refused)


# --------------------------------------------------------------------------------------------
# #770 — a book that must go FLAT has no pairing to guess
# --------------------------------------------------------------------------------------------
#
# MEASURED ON PAPER 2026-09-01. CGAU.XNYS: BCTROT-004 +168, MOMENTUM-002 -88, EXTERNAL -80, all at
# basis 23.65, cache net 0, and the broker holds NONE. Three legs, so `plan_contra_closes` refused
# it as "the correct pairing is not knowable" — and that refusal is right whenever a RESIDUAL
# survives, because then the pairing decides which lane keeps it.
#
# It is NOT right here. When the broker holds nothing and the cache nets to nothing, EVERY leg must
# close. There is no residual to award, so there is no choice being made: 168 = 88 + 80 is the only
# outcome, and each leg closes at ITS OWN basis, so no pairing realizes anything either.
#
# HALO is the counter-case and must STAY refused: +55/-37/+1 against a broker holding 19 leaves a
# residual, and which lane keeps those 19 shares is exactly the guess this module refuses to make.


def _flat_three_legged(inst="CGAU.XNYS"):
    """The live CGAU shape. Sums to zero, and the broker holds none of it."""
    return [
        _Pos(inst, "BCTROT-004", 168.0, 23.65, f"{inst}-BCTROT-004"),
        _Pos(inst, "MOMENTUM-002", -88.0, 23.65, f"{inst}-MOMENTUM-002"),
        _Pos(inst, "EXTERNAL", -80.0, 23.65, f"{inst}-EXTERNAL"),
    ]


def test_the_flat_fixture_really_does_sum_to_zero_and_have_three_legs():
    """FIXTURE PROPERTY FIRST. If it did not net to zero, the new branch could not bind and every
    assertion below would pass with the mechanism removed; if it had two legs it would be planned
    by the ordinary path and prove nothing about three."""
    legs = _flat_three_legged()
    assert len(legs) == 3
    assert sum(p.signed_qty for p in legs) == 0.0


def test_a_three_legged_book_that_must_go_FLAT_is_planned():
    plan = plan_contra_closes(_flat_three_legged(), broker={}, broker_complete=True)
    assert [r.reason for r in plan.refused] == []
    # Two pairs are needed to close three legs, and together they must retire every share.
    assert len(plan.pairs) == 2
    assert {p.instrument_id for p in plan.pairs} == {"CGAU.XNYS"}
    assert sum(p.quantity for p in plan.pairs) == 168.0
    # Every leg is named, and the long side is the only long there is.
    assert {p.long_strategy for p in plan.pairs} == {"BCTROT-004"}
    assert {p.short_strategy for p in plan.pairs} == {"MOMENTUM-002", "EXTERNAL"}
    for p in plan.pairs:
        assert p.broker_qty == 0.0


def test_a_three_legged_book_with_a_RESIDUAL_is_still_refused():
    """HALO. The broker holds 19, so somebody keeps 19, and the position book cannot say who."""
    legs = [
        _Pos("HALO.XNAS", "BCTROT-004", 55.0, 106.42, "HALO.XNAS-BCTROT-004"),
        _Pos("HALO.XNAS", "EXTERNAL", -37.0, 111.78, "HALO.XNAS-EXTERNAL"),
        _Pos("HALO.XNAS", "MOMENTUM-002", 1.0, 104.35, "HALO.XNAS-MOMENTUM-002"),
    ]
    plan = plan_contra_closes(legs, broker={"HALO.XNAS": 19.0}, broker_complete=True)
    assert plan.pairs == ()
    assert any("more than two open legs" in r.reason for r in plan.refused)


def test_a_cache_that_nets_flat_but_a_broker_that_does_NOT_is_refused():
    """The flatten branch must key on the BROKER holding nothing, not merely on the cache summing to
    zero. A broker that still holds shares while the cache nets flat is a net disagreement, and
    closing every leg here would walk the book AWAY from the venue."""
    plan = plan_contra_closes(_flat_three_legged(), broker={"CGAU.XNYS": 40.0}, broker_complete=True)
    assert plan.pairs == ()
    assert any("does not agree with the broker" in r.reason or "more than two" in r.reason
               for r in plan.refused)


def test_GMAB_shape_a_broker_holding_NONE_against_a_cache_that_does_NOT_net_flat():
    """GMAB: +59/-59/+59 = +59 against a broker holding none. The cache holds 59 shares that do not
    exist, which is a NET problem, not a split — closing pairs here would leave the phantom 59."""
    legs = [
        _Pos("GMAB.XNAS", "BCTROT-004", 59.0, 33.41, "GMAB.XNAS-BCTROT-004"),
        _Pos("GMAB.XNAS", "EXTERNAL", -59.0, 33.68, "GMAB.XNAS-EXTERNAL"),
        _Pos("GMAB.XNAS", "MOMENTUM-002", 59.0, 33.94, "GMAB.XNAS-MOMENTUM-002"),
    ]
    plan = plan_contra_closes(legs, broker={}, broker_complete=True)
    assert plan.pairs == (), "pairing would retire 59 and STRAND 59 in whichever lane it spared"
    # #779 resolves this shape by EVICTION rather than by refusing forever. This assertion was
    # written before that existed and said `assert plan.refused`; it and the eviction spec test
    # contradicted each other on byte-identical input, so the suite could not go green under any
    # implementation. The docstring's intent was always anti-PAIRING, not pro-refusal.
    assert plan.evictions



# --------------------------------------------------------------------------------------------
# #779 — when the VENUE holds none of it, every leg is fiction and is EVICTED
# --------------------------------------------------------------------------------------------
#
# MEASURED, paper 2026-09-01. GMAB.XNAS: BCTROT-004 +59 @33.41, MOMENTUM-002 +59 @33.94,
# EXTERNAL -59 @33.68, cache net +59 — and the broker holds ZERO. Its full fill ledger is
# buys 26+33 @33.93/33.95 and 12+47 @33.41, sells 59 @32.57 and 59 @32.59. Net 0, and the production
# FIFO matcher returns NO OPEN BOOK. EXTERNAL's 33.68 is (33.41*59 + 33.94*59)/118 — a fabricated
# reconciliation average, never a traded price.
#
# A PAIR CANNOT EXPRESS THIS. 118 of long against 59 of short retires at most 59 and leaves a lane
# holding 59 phantom shares, which is why this instrument survived #770 and #771. An EVICTION carries
# UNPAIRED legs: each position closes against nothing, justified by the venue saying there is nothing
# there. `contra_execute.Leg` and `RepairOrder.legs` already express that shape.
#
# THE VENUE MUST HAVE SAID ZERO, not merely been silent — evicting on silence is "absence read as
# permission", the exact defect #771 removed from this same function.


def _venue_holds_nothing(inst="GMAB.XNAS"):
    """The live GMAB shape: longs exceeding the shorts, and nothing at the venue."""
    return [
        _Pos(inst, "BCTROT-004", 59.0, 33.41, f"{inst}-BCTROT-004"),
        _Pos(inst, "MOMENTUM-002", 59.0, 33.94, f"{inst}-MOMENTUM-002"),
        _Pos(inst, "EXTERNAL", -59.0, 33.68, f"{inst}-EXTERNAL"),
    ]


def test_the_eviction_fixture_does_NOT_net_flat_so_no_earlier_branch_can_serve_it():
    """FIXTURE PROPERTY FIRST, and it is the whole point: a book that netted to zero would be handled
    by the #770 flatten branch, and every assertion below would pass with eviction removed."""
    legs = _venue_holds_nothing()
    assert sum(p.signed_qty for p in legs) == 59.0
    longs = sum(p.signed_qty for p in legs if p.signed_qty > 0)
    shorts = -sum(p.signed_qty for p in legs if p.signed_qty < 0)
    assert longs != shorts, "pairing could retire everything; then this tests nothing"


def test_EVERY_leg_is_evicted_when_the_venue_holds_NOTHING():
    plan = plan_contra_closes(_venue_holds_nothing(), broker={}, broker_complete=True)
    assert [r.reason for r in plan.refused] == []
    assert plan.pairs == (), "an eviction is not a pair; pairing here would strand 59 shares"
    ev = plan.evictions
    assert len(ev) == 1
    e = ev[0]
    assert e.instrument_id == "GMAB.XNAS"
    # EVERY leg, longs and shorts alike, each at its own basis and its own READ position id.
    assert {(l.strategy_id, l.quantity, l.side) for l in e.legs} == {
        ("BCTROT-004", 59.0, "SELL"),
        ("MOMENTUM-002", 59.0, "SELL"),
        ("EXTERNAL", 59.0, "BUY"),
    }
    assert {l.position_id for l in e.legs} == {
        "GMAB.XNAS-BCTROT-004", "GMAB.XNAS-MOMENTUM-002", "GMAB.XNAS-EXTERNAL"}
    assert {l.strategy_id: l.price for l in e.legs} == {
        "BCTROT-004": 33.41, "MOMENTUM-002": 33.94, "EXTERNAL": 33.68}


def test_eviction_requires_the_venue_to_have_SAID_zero_not_merely_been_silent():
    """An unreadable venue is not a venue holding nothing."""
    plan = plan_contra_closes(_venue_holds_nothing(), broker={})
    assert plan.evictions == ()
    assert plan.pairs == ()
    assert plan.refused


def test_a_venue_that_holds_SOMETHING_is_never_evicted():
    """HALO. The venue reports 19, so a residual must be awarded to a lane, and that is the guess
    this module refuses to make."""
    legs = [
        _Pos("HALO.XNAS", "BCTROT-004", 55.0, 106.42, "HALO.XNAS-BCTROT-004"),
        _Pos("HALO.XNAS", "EXTERNAL", -37.0, 111.78, "HALO.XNAS-EXTERNAL"),
        _Pos("HALO.XNAS", "MOMENTUM-002", 1.0, 104.35, "HALO.XNAS-MOMENTUM-002"),
    ]
    plan = plan_contra_closes(legs, broker={"HALO.XNAS": 19.0}, broker_complete=True)
    assert plan.evictions == ()
    assert plan.pairs == ()
    assert any("more than two open legs" in r.reason for r in plan.refused)


def test_a_TWO_legged_book_the_venue_holds_none_of_is_also_evicted():
    """The rule is about the VENUE holding nothing, not the leg count. A two-leg book with a
    surviving long must not slip onto the ordinary pairing path and leave the phantom standing."""
    legs = [
        _Pos("ZZZ.XNAS", "BCTROT-004", 40.0, 10.0, "ZZZ.XNAS-BCTROT-004"),
        _Pos("ZZZ.XNAS", "EXTERNAL", -25.0, 10.0, "ZZZ.XNAS-EXTERNAL"),
    ]
    plan = plan_contra_closes(legs, broker={}, broker_complete=True)
    assert plan.pairs == ()
    assert len(plan.evictions) == 1
    assert sum(l.quantity for l in plan.evictions[0].legs) == 65.0


def test_the_erased_realized_of_UNPAIRED_legs_is_RECORDED_not_discarded():
    """GMAB's shares really were sold, at 32.57 and 32.59, against bases 33.94 and 33.41. That loss
    belongs to no lane ledger and no zero-realizing close can place it. `unbooked_realized` on a
    ContraPair only models pairs, so an eviction must carry its own — a repair that silently drops
    the number leaves the lane sums and the broker plane disagreeing by exactly it, forever."""
    plan = plan_contra_closes(_venue_holds_nothing(), broker={}, broker_complete=True)
    e = plan.evictions[0]
    assert e.evicted_quantity == 177.0          # 59 + 59 + 59, every leg
    # PINNED TO ITS DERIVATION, not merely non-zero: `!= 0.0` survives a sign flip, a dropped leg
    # (-3,973.65) and a doubled leg. This is (short bases) - (long bases), the same quantity
    # ContraPair records, generalized.
    assert e.unbooked_realized == pytest.approx(59 * 33.68 - 59 * (33.41 + 33.94))
    assert "GMAB" in plan.summary and "evict" in plan.summary.lower()


def test_the_eviction_fingerprint_moves_when_ANY_leg_field_moves():
    """Approval is of a FIXED LIST. Every field whose movement changes the repair must change the
    digest — otherwise a moved book reuses an approval nobody gave for it.

    Each perturbation below is killed by deleting that field from `fingerprint`'s `raw` string."""
    base = plan_contra_closes(_venue_holds_nothing(), broker={}, broker_complete=True).evictions[0]
    from dataclasses import replace

    def rebuilt(**kw):
        return replace(base, **kw).fingerprint

    legs = list(base.legs)
    assert rebuilt(legs=tuple([replace(legs[0], position_id="GMAB.XNAS-OTHER")] + legs[1:])) != base.fingerprint
    assert rebuilt(legs=tuple([replace(legs[0], quantity=58.0)] + legs[1:])) != base.fingerprint
    assert rebuilt(legs=tuple([replace(legs[0], price=1.23)] + legs[1:])) != base.fingerprint
    assert rebuilt(legs=tuple([replace(legs[0], side="BUY")] + legs[1:])) != base.fingerprint
    assert rebuilt(ts_opened=(999,) + base.ts_opened[1:]) != base.fingerprint
    assert rebuilt(broker_qty=7.0) != base.fingerprint
    # A leg VANISHING is a different book, not a smaller one.
    assert rebuilt(legs=tuple(legs[1:]), ts_opened=base.ts_opened[1:]) != base.fingerprint
    # Same book, same digest — otherwise an eviction refuses itself on a second read.
    assert rebuilt() == base.fingerprint


def test_the_eviction_fingerprint_is_DOMAIN_TAGGED_so_it_cannot_collide_with_a_pair():
    """`ContraLedger` keys on the fingerprint ALONE, so a pair/eviction collision must be
    structurally impossible rather than improbable.

    THE OBVIOUS TEST IS VACUOUS. Comparing an eviction's digest to some pair's digest passes with
    the prefix DELETED, because their contents differ anyway — measured: that version survived the
    mutation. So the derivation itself is pinned here, which is the only thing the prefix appears in.
    """
    import hashlib

    ev = plan_contra_closes(_venue_holds_nothing(), broker={}, broker_complete=True).evictions[0]
    lines = "|".join(
        f"{l.strategy_id}|{l.position_id}|"
        f"{l.quantity if l.side == 'SELL' else -l.quantity}|{l.price}|{ts}"
        for l, ts in zip(ev.legs, ev.ts_opened)
    )
    expected = f"EVICT|{ev.instrument_id}|{ev.broker_qty}|{lines}"
    assert ev.fingerprint == hashlib.sha256(expected.encode()).hexdigest()[:16]
    # And the untagged form must NOT produce it — that is the collision the prefix prevents.
    untagged = f"{ev.instrument_id}|{ev.broker_qty}|{lines}"
    assert ev.fingerprint != hashlib.sha256(untagged.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------------------------
# #784 — an OPERATOR may name the pairing the planner refuses to guess
# --------------------------------------------------------------------------------------------
#
# `plan_contra_closes` refuses a book like HALO — BCTROT-004 +55, MOMENTUM-002 +1, EXTERNAL -37
# against a venue holding 19 — because a RESIDUAL survives and which lane keeps it cannot be derived
# from the position book. That refusal is correct and stays: the SYSTEM must not guess.
#
# AN OPERATOR IS NOT GUESSING. Choosing which lane gives back the shares is exactly the decision a
# claim control exists to let a human make. So the pairing becomes an INPUT, and everything else --
# the aim checks, the fingerprint, the all-or-nothing booking -- is the path already proven on CGAU
# and GMAB today.
#
# WHAT IS STILL NOT NEGOTIABLE: the net must not move, the legs must exist on the sides named, the
# quantity must fit both, and the venue must have been read. An operator may choose the SPLIT; they
# may not choose the TOTAL.


def _halo():
    """The live HALO shape: a residual the position book cannot attribute."""
    return [
        _Pos("HALO.XNAS", "BCTROT-004", 55.0, 106.42, "HALO.XNAS-BCTROT-004"),
        _Pos("HALO.XNAS", "MOMENTUM-002", 1.0, 104.35, "HALO.XNAS-MOMENTUM-002"),
        _Pos("HALO.XNAS", "EXTERNAL", -37.0, 111.78, "HALO.XNAS-EXTERNAL"),
    ]


def test_the_fixture_is_one_the_AUTOMATIC_planner_refuses():
    """FIXTURE PROPERTY FIRST. If the automatic planner already handled this shape, an
    operator-directed path would be solving nothing."""
    auto = plan_contra_closes(_halo(), broker={"HALO.XNAS": 19.0}, broker_complete=True)
    assert auto.pairs == () and auto.evictions == ()
    assert any("more than two open legs" in r.reason for r in auto.refused)


def test_an_operator_may_NAME_the_pairing_the_planner_will_not_guess():
    plan = plan_operator_pair(
        _halo(), broker={"HALO.XNAS": 19.0}, broker_complete=True,
        instrument_id="HALO.XNAS", long_strategy="BCTROT-004",
        short_strategy="EXTERNAL", quantity=37.0,
    )
    assert [r.reason for r in plan.refused] == []
    assert len(plan.pairs) == 1
    p = plan.pairs[0]
    assert (p.long_strategy, p.short_strategy, p.quantity) == ("BCTROT-004", "EXTERNAL", 37.0)
    assert p.long_position_id == "HALO.XNAS-BCTROT-004"
    assert p.short_position_id == "HALO.XNAS-EXTERNAL"
    # Each leg at its OWN basis, so the close realizes nothing.
    assert (p.long_px, p.short_px) == (106.42, 111.78)
    # A survivor remains and must be flagged: its basis stays a blend no close can repair.
    assert p.surviving_long_qty == 18.0 and p.is_partial


def test_the_operator_may_choose_the_SPLIT_but_never_the_TOTAL():
    """Closing q from a long and q from a short preserves the net by construction. A quantity that
    exceeds either leg would move it, and is refused rather than clamped — resizing to a number
    nobody approved is what the fingerprint exists to prevent."""
    for qty in (38.0, 56.0):
        plan = plan_operator_pair(
            _halo(), broker={"HALO.XNAS": 19.0}, broker_complete=True,
            instrument_id="HALO.XNAS", long_strategy="BCTROT-004",
            short_strategy="EXTERNAL", quantity=qty,
        )
        assert plan.pairs == (), f"qty {qty} should not fit"
        assert any("does not fit" in r.reason for r in plan.refused)


def test_a_leg_the_operator_NAMED_must_actually_exist_on_that_side():
    """Naming MOMENTUM as the SHORT when it holds a long is an operator error, and must be refused
    by name rather than silently repaired into something else."""
    plan = plan_operator_pair(
        _halo(), broker={"HALO.XNAS": 19.0}, broker_complete=True,
        instrument_id="HALO.XNAS", long_strategy="BCTROT-004",
        short_strategy="MOMENTUM-002", quantity=1.0,
    )
    assert plan.pairs == ()
    assert any("does not hold a SHORT" in r.reason for r in plan.refused)


def test_the_venue_must_still_have_been_READ():
    """An operator may choose a split; they may not waive knowing what the venue holds. Silence is
    not a reading."""
    plan = plan_operator_pair(
        _halo(), broker={},
        instrument_id="HALO.XNAS", long_strategy="BCTROT-004",
        short_strategy="EXTERNAL", quantity=37.0,
    )
    assert plan.pairs == ()
    assert any("did not mention" in r.reason for r in plan.refused)


def test_a_resting_order_still_blocks_an_operator_directed_pair():
    """#239 is not waived by an operator naming the pairing: a reduce-only order sized to the
    pre-repair quantity oversells on trigger just the same."""
    plan = plan_operator_pair(
        _halo(), broker={"HALO.XNAS": 19.0}, broker_complete=True,
        working_orders=["HALO.XNAS"], known_instrument_ids=["HALO.XNAS"],
        instrument_id="HALO.XNAS", long_strategy="BCTROT-004",
        short_strategy="EXTERNAL", quantity=37.0,
    )
    assert plan.pairs == ()
    assert any("resting order" in r.reason for r in plan.refused)
