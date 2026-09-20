"""Whose stop is that? (#748, lane-aware coverage.)

`unprotected_positions` credits coverage PRO-RATA: it measures protection per INSTRUMENT and then
multiplies by each lane's share of the quantity. That is the right answer to the question it was
built for — "is the book covered" — and the wrong one for the question the per-lane split asks.

THE FAILURE IT PRODUCES. MOMENTUM holds 54 with its own 54-share stop resting; MANUAL holds 2 with
none. Instrument coverage is 54 against 56 held, so MOMENTUM is told it is 1.93 uncovered when it is
covered exactly, and MANUAL 0.07 when it is naked for all 2. A planner building per-lane stops from
those numbers rests a second ~1.93 MOMENTUM stop — lane-level OVER-coverage, which flips the lane
short on trigger — and a 0.07 MANUAL stub that protects nothing.

THE JOIN IS THE SAME SHAPE AS THE POSITION SPLIT, for the same reason. The broker says WHICH orders
are resting and is the only trustworthy source for that (#285: the cache once held three orders as
REJECTED that Alpaca reported `new`, and a cache-based coverage check answers "nothing resting", so
the next pass places a duplicate stop). The cache says WHOSE they are, because a client order id is
all the broker row carries and the lane is not recoverable from it.

AND AN ORDER THE CACHE CANNOT NAME MAKES THE INSTRUMENT'S SPLIT UNKNOWN, not zero. Dropping it would
under-count coverage and rest a duplicate stop; crediting it to a guessed lane is the defect this
whole ticket is about.
"""

from __future__ import annotations

from api.lane_coverage import coverage_by_lane
from api.protection import protective_quantity


def _stop(coid, qty=54.0, symbol="AEM", side="sell", otype="trailing_stop", status="new"):
    return {"symbol": symbol, "side": side, "qty": qty, "order_type": otype,
            "status": status, "filled_qty": 0.0, "client_order_id": coid}


def _owners(mapping):
    return lambda coid: mapping.get(coid)


def test_each_lane_is_credited_with_ITS_OWN_resting_stop():
    orders = [_stop("PROT-SELL-AEM-1", 54.0), _stop("PROT-SELL-AEM-2", 2.0)]
    cov = coverage_by_lane(orders, "AEM.XNYS", "sell",
                           _owners({"PROT-SELL-AEM-1": "MOMENTUM-002",
                                    "PROT-SELL-AEM-2": "MANUAL-001"}))
    assert cov.by_lane == {"MOMENTUM-002": 54.0, "MANUAL-001": 2.0}
    assert cov.unattributed == 0.0
    assert cov.is_complete is True


def test_THE_PRO_RATA_ANSWER_AND_THIS_ONE_DISAGREE_and_that_is_the_whole_point():
    """The two derivations must agree on the TOTAL and differ on the SPLIT. If they agreed on the
    split there would be nothing to fix; if they disagreed on the total, one of them is wrong about
    the book rather than about attribution."""
    orders = [_stop("PROT-SELL-AEM-1", 54.0)]
    owner = _owners({"PROT-SELL-AEM-1": "MOMENTUM-002"})
    cov = coverage_by_lane(orders, "AEM.XNYS", "sell", owner)

    # SAME TOTAL as the instrument-level function — two derivations of one fact, pinned to agree.
    assert sum(cov.by_lane.values()) + cov.unattributed == protective_quantity(
        orders, "AEM.XNYS", "sell")

    # DIFFERENT SPLIT. Pro-rata over 56 held would give MOMENTUM 54*(54/56) = 52.07 and MANUAL 1.93.
    assert cov.by_lane == {"MOMENTUM-002": 54.0}
    assert cov.by_lane["MOMENTUM-002"] != 54.0 * (54.0 / 56.0)


def test_a_resting_order_THE_CACHE_CANNOT_NAME_makes_the_split_INCOMPLETE():
    """Three states. Dropping it under-counts coverage and rests a duplicate stop; guessing an owner
    is the defect the ticket exists to end. So it is counted, kept out of the per-lane map, and the
    instrument is marked as one whose split cannot be trusted."""
    orders = [_stop("PROT-SELL-AEM-1", 54.0), _stop("SOMEONE-ELSES-ORDER", 2.0)]
    cov = coverage_by_lane(orders, "AEM.XNYS", "sell",
                           _owners({"PROT-SELL-AEM-1": "MOMENTUM-002"}))
    assert cov.by_lane == {"MOMENTUM-002": 54.0}
    assert cov.unattributed == 2.0
    assert cov.is_complete is False


def test_the_TOTAL_still_agrees_when_part_of_it_is_unattributed():
    """The unattributed quantity is not lost — that is what makes it safe to refuse rather than
    guess. A version that dropped it would silently under-count and rest a duplicate stop."""
    orders = [_stop("PROT-SELL-AEM-1", 54.0), _stop("MYSTERY", 2.0)]
    cov = coverage_by_lane(orders, "AEM.XNYS", "sell", _owners({"PROT-SELL-AEM-1": "MOMENTUM-002"}))
    assert sum(cov.by_lane.values()) + cov.unattributed == protective_quantity(
        orders, "AEM.XNYS", "sell")


def test_a_stop_owned_by_a_lane_that_holds_NOTHING_is_still_ITS_coverage():
    """Coverage is about which shares are RESERVED, not about who ought to hold them. A stop left
    behind by a lane that has since exited still reserves shares at the venue, and pretending it
    belongs to whoever holds the position is exactly the guess being removed."""
    orders = [_stop("PROT-SELL-AEM-1", 2.0)]
    cov = coverage_by_lane(orders, "AEM.XNYS", "sell", _owners({"PROT-SELL-AEM-1": "MANUAL-001"}))
    assert cov.by_lane == {"MANUAL-001": 2.0}


def test_NO_resting_orders_is_COMPLETE_and_empty_not_unknown():
    """Nothing resting is a known state — every lane is uncovered — and must not be confused with
    a read that could not attribute what it found. Reporting it as incomplete would send every clean
    instrument down the fallback and disable the split everywhere."""
    cov = coverage_by_lane([], "AEM.XNYS", "sell", _owners({}))
    assert cov.by_lane == {} and cov.unattributed == 0.0
    assert cov.is_complete is True


def test_a_NON_PROTECTIVE_order_is_not_counted_as_coverage_by_either_derivation():
    """A take-profit limit above the market does nothing on the way down. `protective_orders` already
    encodes that judgement, so this reuses it rather than re-deciding — two derivations of "what
    counts as protection" would drift, and that is the subject of this whole area."""
    orders = [_stop("TP-AEM-1", 54.0, otype="limit")]
    cov = coverage_by_lane(orders, "AEM.XNYS", "sell", _owners({"TP-AEM-1": "MOMENTUM-002"}))
    assert cov.by_lane == {} and cov.unattributed == 0.0
    assert protective_quantity(orders, "AEM.XNYS", "sell") == 0.0


def test_an_order_on_ANOTHER_INSTRUMENT_is_not_counted():
    orders = [_stop("PROT-SELL-NVDA-1", 54.0, symbol="NVDA")]
    cov = coverage_by_lane(orders, "AEM.XNYS", "sell", _owners({"PROT-SELL-NVDA-1": "MOMENTUM-002"}))
    assert cov.by_lane == {} and cov.is_complete is True


def test_TWO_STOPS_OWNED_BY_ONE_LANE_ACCUMULATE_rather_than_overwrite():
    """Measured on a live instance paper book, so this is the ordinary case and not an edge one.

    The planner sizes only the SHORTFALL, so a lane that added shares gets a SECOND stop covering the
    difference rather than a resized first one. Right now SSRM rests 53 + 51 against 104 held, GMAB
    59 + 59 against 118, AEM 9 + 9 against 18. A map that overwrote instead of accumulating would
    report SSRM's owner as covered for 51 of its 104 — and the planner would rest a third stop for
    the 53 that is already reserved, which is `403 insufficient qty available`.
    """
    orders = [_stop("PROT-SELL-SSRM-1", 53.0, symbol="SSRM"),
              _stop("PROT-SELL-SSRM-2", 51.0, symbol="SSRM")]
    cov = coverage_by_lane(orders, "SSRM.XNAS", "sell",
                           _owners({"PROT-SELL-SSRM-1": "MOMENTUM-002",
                                    "PROT-SELL-SSRM-2": "MOMENTUM-002"}))
    assert cov.by_lane == {"MOMENTUM-002": 104.0}
    assert cov.total == protective_quantity(orders, "SSRM.XNAS", "sell")


def test_a_PARTIALLY_FILLED_stop_only_reserves_its_REMAINDER():
    """A stop that has already sold half of what it covers is holding half the shares, not all of
    them. Counting the full quantity reports coverage that no longer exists and leaves the remainder
    naked — presence standing in for protection, which is the defect `unprotected_positions` was
    built to end one level up."""
    orders = [_stop("PROT-SELL-AEM-1", 54.0)]
    orders[0]["filled_qty"] = 20.0
    cov = coverage_by_lane(orders, "AEM.XNYS", "sell", _owners({"PROT-SELL-AEM-1": "MOMENTUM-002"}))
    assert cov.by_lane == {"MOMENTUM-002": 34.0}


def test_an_order_with_NO_CLIENT_ORDER_ID_is_unattributed_NOT_defaulted():
    """A BRACKET LEG CARRIES AN ID ALPACA GENERATED, not ours — the repo already documents this: we
    submit a bracket as one native request and the venue mints the child ids. So an empty or foreign
    client order id is a real shape on this book, and it is precisely the one where a default would
    be invisible.

    Defaulting it to the submitting strategy is the whole #748 defect expressed in a different
    module: an owner asserted rather than known. It is unattributed, and the instrument's split is
    incomplete.
    """
    orders = [_stop("", 54.0)]
    cov = coverage_by_lane(orders, "AEM.XNYS", "sell", _owners({}))
    assert cov.by_lane == {}
    assert cov.unattributed == 54.0
    assert cov.is_complete is False


# ==================================================================================================
# THE `owner_of` CONTRACT — pinned BEFORE the producer exists, because this is where it would be
# inherited wrong.
# ==================================================================================================
def test_a_PREFIX_STYLE_owner_of_is_CONFIDENTLY_WRONG_IN_BOTH_DIRECTIONS_AT_ONCE():
    """DO NOT BUILD THE PRODUCER OUT OF `engine_node._owner_of`. This test is the reason.

    That helper falls back to the client-order-id PREFIX: a `PROT-` order is account protection, so
    MANUAL-001 may touch it. For CANCEL AUTHORISATION that is the safe direction — the question is
    "may this caller cancel it", and answering yes for account protection is deliberate.

    FOR COVERAGE ATTRIBUTION THE SAME FALLBACK IS POISON, because the question is "whose shares are
    reserved" and a prefix cannot know. Take a cache-terminal per-lane stop — genuinely MOMENTUM's,
    invisible to the cache, the shape behind the InvalidStateTrigger incident. A prefix-style
    owner_of names it MANUAL-001, and the result is wrong in BOTH directions simultaneously:

        MOMENTUM reads naked        -> the next pass rests a DUPLICATE stop over reserved shares,
                                       which oversells on trigger
        MANUAL reads over-covered   -> lane-aware oversize shrinks a stop that is not there to shrink

    and `is_complete` is True throughout, so nothing reports a problem. An honest owner_of returning
    None yields `unattributed=54, is_complete=False` — the refuse-to-split signal this module exists
    to emit.

    THE CONTRACT: answer from the CACHE ORDER only — venue id, then client order id,
    `order.strategy_id` — and return None when only the prefix knows. One lookup, two questions,
    opposite fallback semantics; a shared implementation needs two entry points or a flag, and the
    coverage entry point must NEVER return a lane the cache did not name.
    """
    orders = [_stop("PROT-SELL-AEM-1", 54.0)]

    def prefix_style(coid):
        return "MANUAL-001" if coid.startswith("PROT-") else None

    honest = _owners({})            # the cache does not know this order

    poisoned = coverage_by_lane(orders, "AEM.XNYS", "sell", prefix_style)
    truthful = coverage_by_lane(orders, "AEM.XNYS", "sell", honest)

    # The two derivations DISAGREE, and the disagreement is the whole finding.
    assert poisoned.by_lane == {"MANUAL-001": 54.0} and poisoned.is_complete is True
    assert truthful.by_lane == {} and truthful.unattributed == 54.0
    assert truthful.is_complete is False, (
        "an order the cache cannot name must make the split incomplete — if this ever passes with "
        "is_complete True, the producer has grown a fallback and the refuse-to-split signal is gone"
    )


def test_a_QUANTITY_KEYED_row_is_seen_the_SAME_as_a_QTY_KEYED_one():
    """Coverage was parsed monolingually while the rows it reads are bilingual.

    `protective_orders` builds its rows from `o.get("qty") or o.get("quantity")` — deliberately, this
    repo's frames speak `quantity` and the broker speaks `qty` — and hands back a `_remaining` it has
    already computed. Reading `order["qty"]` again re-derives that fact in ONE language, so a
    `quantity`-keyed protective row yields `by_lane={}`, `unattributed=0.0`, and `is_complete=True`
    with the shares resting UNSEEN.

    That is the worst possible failure for this module: the vacuity is in the QUANTITY PARSE, not in
    attribution, so the unattributed channel — the whole refuse-to-split signal — stays silent while
    the answer is confidently wrong. A planner reading it rests a duplicate stop over shares already
    reserved.

    THE SECOND DERIVATION IS THE BUG. `_remaining` is the number `protective_orders` exists to
    produce; recomputing it here is exactly what this module's own docstring warns against doing to
    the "what counts as protection" judgement, one field over.
    """
    ours = {"symbol": "AEM", "side": "sell", "quantity": 54.0, "order_type": "trailing_stop",
            "status": "new", "filled_qty": 0.0, "client_order_id": "PROT-SELL-AEM-1"}
    theirs = _stop("PROT-SELL-AEM-1", 54.0)

    # FIXTURE PROPERTY FIRST: the two rows must genuinely differ in which key carries the size.
    assert "qty" not in ours and "quantity" in ours
    assert "qty" in theirs and "quantity" not in theirs

    owner = _owners({"PROT-SELL-AEM-1": "MOMENTUM-002"})
    assert coverage_by_lane([ours], "AEM.XNYS", "sell", owner).by_lane == {"MOMENTUM-002": 54.0}
    assert coverage_by_lane([theirs], "AEM.XNYS", "sell", owner).by_lane == {"MOMENTUM-002": 54.0}
