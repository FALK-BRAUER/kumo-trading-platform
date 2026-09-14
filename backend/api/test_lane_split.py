"""Who holds the shares the broker says we hold? (#748, the per-lane source.)

THE PROBLEM THIS EXISTS FOR. Protective stops are stamped with the strategy that BUILT them, and
under NETTING a fill's position is derived as `{instrument}-{fill.strategy_id}`. A stop built by the
display strategy therefore resolves to a position that has never existed, its reduce-only fill lands
on nothing, and the position poll fabricates the difference — the phantom mint. Fixing it means
resting one stop per HOLDING LANE, and that needs a per-lane view of the book.

THE BROKER DOES NOT HAVE ONE. `broker_rows` sets `strategy_id: ""` and says why: the broker does not
know about our sleeves. The engine cache does know, but the protection sweep deliberately plans from
BROKER state (#285) — on 2026-08-14 the engine held three orders as REJECTED that Alpaca reported
`new`, and a cache-based coverage check answers "nothing resting", so the next pass places a
DUPLICATE stop.

SO THE TWO PLANES ARE JOINED, EACH DOING WHAT ONLY IT CAN. The broker anchors the TOTAL, which is
this repo's stated architecture — broker net is the only hard reconciliation anchor and the
per-strategy split is unverified by it. The cache supplies the SPLIT. And where the two disagree the
instrument is NOT split, because the disagreement means the per-lane numbers do not describe the
shares the venue is holding, and splitting on them would attribute real shares to a lane that does
not hold them.

THAT LAST CLAUSE IS THE UNCOMFORTABLE ONE AND IT IS DELIBERATE. The per-lane split is corrupted by
exactly the phantoms this work exists to stop, so a symbol carrying a phantom leg falls back to one
aggregate row and keeps today's behaviour until #744 repairs it. That is the correct ordering, not a
workaround.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.lane_split import split_by_lane


@dataclass
class _Pos:
    instrument_id: str
    strategy_id: str
    signed_qty: float
    is_open: bool = True


def _broker(iid="AEM.XNYS", qty=56.0, mv=5600.0, side="LONG"):
    return {"instrument_id": iid, "quantity": abs(qty), "side": side,
            "market_value": mv, "strategy_id": ""}


def test_a_book_whose_lanes_SUM_TO_THE_BROKER_is_split_per_lane():
    """The whole point: 56 held, 54 by MOMENTUM and 2 by MANUAL, becomes two rows."""
    split = split_by_lane(
        [_broker(qty=56.0, mv=5600.0)],
        [_Pos("AEM.XNYS", "MOMENTUM-002", 54.0), _Pos("AEM.XNYS", "MANUAL-001", 2.0)],
    )
    assert split.divergent == ()
    by_lane = {r["strategy_id"]: r["quantity"] for r in split.rows}
    assert by_lane == {"MOMENTUM-002": 54.0, "MANUAL-001": 2.0}


def test_the_market_value_is_APPORTIONED_so_the_lane_rows_still_sum_to_the_broker():
    """A row's notional drives refusal reporting and exposure. Splitting the quantity while leaving
    each row the FULL market value would report the exposure twice."""
    split = split_by_lane(
        [_broker(qty=56.0, mv=5600.0)],
        [_Pos("AEM.XNYS", "MOMENTUM-002", 54.0), _Pos("AEM.XNYS", "MANUAL-001", 2.0)],
    )
    assert sum(r["market_value"] for r in split.rows) == 5600.0
    by_lane = {r["strategy_id"]: r["market_value"] for r in split.rows}
    assert by_lane["MOMENTUM-002"] == 5400.0 and by_lane["MANUAL-001"] == 200.0


def test_a_book_that_DISAGREES_with_the_broker_falls_back_to_ONE_AGGREGATE_ROW_and_says_so():
    """THE PHANTOM CASE, and the reason the fallback exists. Cache says +168/-88 (net 80), broker
    says 80 — those agree. Here the cache nets 60 against a broker 56: the per-lane numbers do not
    describe the shares the venue holds, so splitting on them would hand real shares to a lane that
    does not hold them. One aggregate row, exactly today's behaviour, plus a named divergence."""
    split = split_by_lane(
        [_broker(qty=56.0, mv=5600.0)],
        [_Pos("AEM.XNYS", "MOMENTUM-002", 54.0), _Pos("AEM.XNYS", "MANUAL-001", 6.0)],
    )
    assert [r["strategy_id"] for r in split.rows] == [""], split.rows
    assert split.rows[0]["quantity"] == 56.0
    assert len(split.divergent) == 1
    assert split.divergent[0].instrument_id == "AEM.XNYS"
    assert split.divergent[0].cache_net == 60.0 and split.divergent[0].broker_net == 56.0


def test_an_instrument_THE_CACHE_KNOWS_NOTHING_ABOUT_falls_back_and_is_REPORTED():
    """Absence is a timestamp, not a property. No cache legs might mean a seeding frame, a stale
    read, or a genuinely unattributed holding — and silently emitting the aggregate row would make a
    blind read indistinguishable from a healthy one."""
    split = split_by_lane([_broker()], [])
    assert [r["strategy_id"] for r in split.rows] == [""]
    assert len(split.divergent) == 1
    assert split.divergent[0].cache_net == 0.0
    # THE REASON IS THE POINT, not just the fallback. Deleting this branch still produces an
    # aggregate row, because the net comparison catches 0 vs 56 anyway — so the OUTCOME is
    # identical and only the explanation changes, from "the cache knows nothing about this
    # instrument" to "the numbers disagree". Those are different conditions: one is a blind read,
    # the other is real drift, and an operator chasing the second when it is the first looks in
    # the wrong plane. A mutation removing the branch survived until this line existed.
    assert "knows no open position" in split.divergent[0].reason, split.divergent[0].reason


def test_a_CLOSED_cache_position_does_not_count_toward_the_split():
    """The durable cache retains CLOSED positions under the same id shape. Counting them makes the
    cache net disagree with the broker on nearly every symbol, which would send the whole book down
    the aggregate fallback and silently disable the split."""
    split = split_by_lane(
        [_broker(qty=54.0, mv=5400.0)],
        [_Pos("AEM.XNYS", "MOMENTUM-002", 54.0),
         _Pos("AEM.XNYS", "BCTROT-004", 99.0, is_open=False)],
    )
    assert split.divergent == ()
    assert {r["strategy_id"] for r in split.rows} == {"MOMENTUM-002"}


def test_a_position_that_CANNOT_SAY_whether_it_is_open_makes_the_instrument_DIVERGENT():
    """Not skipped, and not counted. A shape this read cannot describe means the split for that
    instrument is unknown — the third state — and the aggregate fallback is what "unknown" looks
    like here."""
    class _Mute:
        instrument_id = "AEM.XNYS"
        strategy_id = "MOMENTUM-002"
        signed_qty = 54.0

    assert not hasattr(_Mute(), "is_open"), "fixture must not carry is_open"
    split = split_by_lane([_broker(qty=54.0)], [_Mute()])
    assert [r["strategy_id"] for r in split.rows] == [""]
    assert split.divergent and "cannot say" in split.divergent[0].reason


def test_a_SHORT_broker_row_splits_on_signed_quantity_not_absolute():
    """The row shape carries an unsigned `quantity` plus a `side`, so the comparison must be made in
    signed space or a short book compares +qty against -net and diverges on every symbol."""
    split = split_by_lane(
        [_broker(qty=30.0, mv=-3000.0, side="SHORT")],
        [_Pos("AEM.XNYS", "MOMENTUM-002", -30.0)],
    )
    assert split.divergent == ()
    assert split.rows[0]["side"] == "SHORT" and split.rows[0]["quantity"] == 30.0


def test_ONE_DIVERGENT_INSTRUMENT_DOES_NOT_SPOIL_THE_OTHERS():
    """Per instrument, not per book. A single phantom-carrying symbol must not send every clean one
    down the fallback — that would make the split useless for exactly as long as any damage exists."""
    split = split_by_lane(
        [_broker("AEM.XNYS", 56.0, 5600.0), _broker("WHD.XNYS", 28.0, 2800.0)],
        [_Pos("AEM.XNYS", "MOMENTUM-002", 54.0), _Pos("AEM.XNYS", "MANUAL-001", 6.0),
         _Pos("WHD.XNYS", "BCTROT-004", 28.0)],
    )
    assert [d.instrument_id for d in split.divergent] == ["AEM.XNYS"]
    lanes = {(r["instrument_id"], r["strategy_id"]) for r in split.rows}
    assert ("WHD.XNYS", "BCTROT-004") in lanes
    assert ("AEM.XNYS", "") in lanes


def test_a_ZERO_QUANTITY_cache_leg_is_not_a_holder():
    """An open position at zero quantity contributes nothing and must not produce a row — a stop for
    zero shares is not a stop, and the lane does not hold the instrument."""
    split = split_by_lane(
        [_broker(qty=54.0, mv=5400.0)],
        [_Pos("AEM.XNYS", "MOMENTUM-002", 54.0), _Pos("AEM.XNYS", "MANUAL-001", 0.0)],
    )
    assert split.divergent == ()
    assert {r["strategy_id"] for r in split.rows} == {"MOMENTUM-002"}


def test_an_EMPTY_broker_book_produces_NO_ROWS_but_DOES_report_the_orphaned_cache_legs():
    """Renamed and reversed. This previously asserted "produces nothing" and pinned the silence in
    the paragraph above as intended — which made the module's own divergence channel blind to the
    starkest cache/broker disagreement there is."""
    split = split_by_lane([], [_Pos("AEM.XNYS", "MOMENTUM-002", 54.0)])
    assert split.rows == ()
    assert [d.instrument_id for d in split.divergent] == ["AEM.XNYS"]


def test_a_ZERO_BROKER_TOTAL_with_an_EMPTY_CACHE_still_falls_back_rather_than_AGREEING():
    """The one arrangement where "all lanes zero" could read as agreement instead of ignorance.

    Against a nonzero broker total an empty cache nets 0 and diverges, which is safe. Against a ZERO
    broker total it nets 0 and AGREES — the fallback never fires, the instrument is reported as
    cleanly split into nothing, and a blind read is indistinguishable from a flat book. That is the
    empty-`_by_symbol` shape that let QC27 resolve its whole pool, report armed, and neither enter
    nor exit.

    The empty-cache branch runs BEFORE the net comparison precisely so the third state cannot be
    collapsed into the first by an arithmetic coincidence. This test is what fails if the two are
    ever reordered.
    """
    split = split_by_lane([_broker(qty=0.0, mv=0.0)], [])
    assert [r["strategy_id"] for r in split.rows] == [""]
    assert split.divergent and "knows no open position" in split.divergent[0].reason


def test_a_NaN_LEG_QUANTITY_makes_the_instrument_DIVERGENT_not_SPLIT():
    """The same NaN family fixed in `contra_close` the same day, one module over — which is the
    point: fixing it where it was found and not where it lives is how a class survives.

    `abs(nan - broker) > tol` is False, so the net check PASSES and the instrument SPLITS, emitting a
    row with `quantity=nan`. The per-row `abs(qty) <= QTY_TOLERANCE` skip is NaN-transparent too, so
    nothing downstream removes it. A NaN row flowing into stop sizing is a stop the venue rejects at
    best, and every comparison written for numbers is False against it — the family that silently
    disarmed a daily-loss halt.
    """
    class _NaNPos:
        instrument_id = "AEM.XNYS"
        strategy_id = "MOMENTUM-002"
        signed_qty = float("nan")
        is_open = True

    split = split_by_lane([_broker(qty=56.0)], [_NaNPos()])
    assert [r["strategy_id"] for r in split.rows] == [""], split.rows
    assert split.divergent and "not a number" in split.divergent[0].reason


def test_a_NaN_BROKER_QUANTITY_makes_the_instrument_DIVERGENT():
    """The other side of the same comparison. A broker row that cannot be read is not a book to
    split against."""
    split = split_by_lane([_broker(qty=float("nan"))],
                          [_Pos("AEM.XNYS", "MOMENTUM-002", 54.0)])
    assert [r["strategy_id"] for r in split.rows] == [""]
    assert split.divergent and "not a number" in split.divergent[0].reason


def test_an_instrument_THE_CACHE_HOLDS_BUT_THE_BROKER_DOES_NOT_is_REPORTED():
    """The starkest possible cache/broker disagreement was leaving the divergence channel EMPTY.

    A broker-flat mirrored pair — WHD +28 in one lane, -28 in another, absent from the broker read —
    is the CORE #744 shape, and it produced zero rows and zero divergences. For stop sizing that is
    defensible: the venue holds nothing to protect. But this module's Divergence channel is the
    designated place for "the cache claims what the broker does not hold", and leaving it silent is
    absence reading as agreement — in the one module built to keep those apart.

    Reported with `broker_net=0.0`, which is the fact: the broker read did not mention it.
    """
    split = split_by_lane(
        [],
        [_Pos("WHD.XNYS", "BCTROT-004", 28.0), _Pos("WHD.XNYS", "MOMENTUM-002", -28.0)],
    )
    assert split.rows == ()
    assert [d.instrument_id for d in split.divergent] == ["WHD.XNYS"]
    # UNKNOWN, not zero — the broker read did not mention this instrument, which is a different fact
    # from it holding none. See `test_an_UNMENTIONED_instrument_reports_broker_net_as_UNKNOWN...`.
    assert split.divergent[0].broker_net is None
    assert split.divergent[0].cache_net == 0.0
    assert "broker read does not mention" in split.divergent[0].reason


def test_an_UNMENTIONED_instrument_reports_broker_net_as_UNKNOWN_not_as_ZERO():
    """0.0 launders "the broker read did not mention it" into "the broker holds none of it".

    Defensible while `Divergence` is only a report — but the moment any consumer does arithmetic on
    it (a /health sum of |broker - cache| gaps, a `broker_net == 0` filter) the two states collapse,
    in the module whose entire purpose is keeping them apart. None costs nothing now and cannot be
    misread later.
    """
    split = split_by_lane([], [_Pos("WHD.XNYS", "BCTROT-004", 28.0)])
    assert split.divergent[0].broker_net is None


def test_an_UNREADABLE_cache_net_is_reported_as_UNKNOWN_not_rewritten_to_ZERO():
    """The same laundering one field over: a NaN cache net was being rewritten to 0.0 inside the
    report, turning "this leg cannot be read" into "this leg is flat"."""
    class _NaNPos:
        instrument_id = "WHD.XNYS"
        strategy_id = "BCTROT-004"
        signed_qty = float("nan")
        is_open = True

    split = split_by_lane([], [_NaNPos()])
    assert split.divergent[0].cache_net is None
