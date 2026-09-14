"""Tests for protective-stop sizing and the naked-position audit (#239).

The numbers here are the real book on 2026-08-13, so a change that would have altered the widths we
reasoned about shows up as a failing assertion rather than a quiet drift.
"""

from __future__ import annotations

from api.protection import (
    atr,
    broker_rows,
    peak_trail_bps,
    plan_protection,
    trail_width,
    true_ranges,
    unprotected_positions,
)


def _bars(rows: list[tuple[float, float, float]]) -> list[dict]:
    """(high, low, close) oldest first."""
    return [{"h": h, "l": lo, "c": c} for h, lo, c in rows]


class TestTrueRange:
    def test_counts_an_overnight_gap_not_just_the_bar(self):
        # The whole reason TR is not simply high-low: a session that gaps down and then trades quietly
        # has a small range but a large true move. Treating it as quiet would size the stop far too
        # tight for a symbol that gaps.
        bars = _bars([(100, 99, 100), (95, 94, 94)])
        assert true_ranges(bars) == [1, 6]  # not [1, 1]

    def test_the_first_bar_has_no_previous_close_to_gap_from(self):
        assert true_ranges(_bars([(100, 90, 95)])) == [10]

    def test_no_bars_is_no_ranges(self):
        assert true_ranges([]) == []


class TestAtr:
    def test_averages_the_lookback_window(self):
        # 15 bars for a 14-session window: the first true range has no previous close to gap from, so a
        # gap-aware window of 14 needs one bar of run-up. (codex review, Medium.)
        assert atr(_bars([(10, 8, 9)] * 15), lookback=14) == 2

    def test_needs_one_extra_bar_so_the_oldest_gap_is_not_dropped(self):
        assert atr(_bars([(10, 8, 9)] * 14), lookback=14) is None
        assert atr(_bars([(10, 8, 9)] * 15), lookback=14) is not None

    def test_uses_only_the_most_recent_window(self):
        # Violent history at the SAME price level — an earlier version of this test jumped the stock from
        # 80 to 9, so the transition bar had a true range of 70 and the average came out at 7. That was
        # the code being right about a gap and the fixture being a stock that does not exist.
        old = [(20, 5, 9)] * 10  # same ~9 close, much wider ranges
        recent = [(10, 8, 9)] * 14
        assert atr(_bars(old + recent), lookback=14) == 2

    def test_returns_None_rather_than_guessing_on_short_history(self):
        # A symbol we cannot measure gets NO automatic stop rather than an arbitrary one — the same
        # "wait for real data, never fabricate" rule the managers follow.
        assert atr(_bars([(10, 8, 9)] * 13), lookback=14) is None
        assert atr([], lookback=14) is None


class TestTrailWidth:
    def test_the_live_book_produces_the_widths_we_reasoned_about(self):
        # Measured 2026-08-13. VFLO is the quietest name on the book and NBIS the most violent; the
        # 7x spread between them is why a single percentage cannot serve both.
        vflo = trail_width(53.42, 0.88, multiple=1.5)
        nbis = trail_width(248.53, 27.78, multiple=1.5)
        assert vflo is not None and nbis is not None
        assert round(vflo.atr_pct, 2) == 1.65
        assert round(nbis.atr_pct, 2) == 11.18
        # A flat 2.5% would be 1.5x ATR on VFLO and 0.22x on NBIS — it would sell NBIS on a normal hour.
        assert vflo.pct < 2.5 < nbis.pct

    def test_a_quiet_symbol_is_held_at_the_floor(self):
        # A stop that fires on noise is worse than none: it converts a paper wobble into a realised loss.
        w = trail_width(100, 0.2, multiple=1.5, min_pct=1.0)
        assert w is not None and w.pct == 1.0 and w.clamped == "floor"

    def test_a_violent_symbol_is_held_at_the_ceiling_and_says_so(self):
        # NBIS at 1.5x ATR wants 16.8%. Past the ceiling the order stops being protection and becomes
        # decoration — the clamp has to be visible, not silent, because it means the position is too
        # large for its volatility.
        w = trail_width(248.53, 27.78, multiple=1.5, max_pct=15.0)
        assert w is not None and w.pct == 15.0 and w.clamped == "ceiling"

    def test_an_ordinary_width_is_not_reported_as_clamped(self):
        w = trail_width(181.95, 6.94, multiple=1.5)
        assert w is not None and w.clamped is None
        assert round(w.pct, 2) == 5.72  # AEM

    def test_converts_to_basis_points_the_way_the_UI_rounds(self):
        # Python's round() is banker's rounding and JavaScript's is not; the settings seam already had
        # to be fixed for this once. 2.505% must be 251 bps on both sides, not 250.
        w = trail_width(100, 1.67, multiple=1.5)  # 2.505%
        assert w is not None and w.bps == 251

    def test_refuses_rather_than_defaults_on_unusable_inputs(self):
        assert trail_width(0, 5) is None
        assert trail_width(100, 0) is None
        assert trail_width(-10, 5) is None
        assert trail_width(100, -5) is None

    def test_the_floor_stays_above_the_venue_minimum(self):
        # Alpaca refuses trail_percent < 0.1 outright (measured 2026-08-12). The configured floor must
        # never produce an order the venue will reject.
        w = trail_width(100, 0.001, multiple=1.5, min_pct=1.0)
        assert w is not None and w.bps >= 10


class TestUnprotectedAudit:
    def _pos(self, iid="AEM.XNYS", qty=54.0, mv=9825.0, strat="MOMENTUM-002"):
        return {"instrument_id": iid, "quantity": qty, "market_value": mv, "strategy_id": strat}

    def _stop(self, sym="AEM.XNYS", qty=54, typ="trailing_stop", side="sell", status="new", filled=0):
        return {"symbol": sym, "qty": qty, "type": typ, "side": side, "status": status, "filled_qty": filled}

    def test_a_position_with_nothing_resting_is_naked(self):
        found = unprotected_positions([self._pos()], [])
        assert len(found) == 1 and found[0].uncovered == 54.0

    def test_a_full_size_protective_stop_covers_it(self):
        assert unprotected_positions([self._pos()], [self._stop()]) == []

    def test_a_TAKE_PROFIT_LIMIT_is_not_protection(self):
        # The finding that mattered. A limit reduces the position but sits ABOVE the market and does
        # nothing on the way down. NBIS carried exactly this shape on 2026-08-12 — a stop AND a limit —
        # and the limit outlived the stop. Counting it would report the book as covered while it was not.
        found = unprotected_positions([self._pos()], [self._stop(typ="limit")])
        assert len(found) == 1 and found[0].uncovered == 54.0

    def test_a_PARTIAL_stop_reports_only_the_shortfall(self):
        # A 1-share stop on 54 shares is not full protection, and it is not zero protection either.
        found = unprotected_positions([self._pos()], [self._stop(qty=1)])
        assert len(found) == 1
        assert found[0].uncovered == 53.0
        assert round(found[0].uncovered_notional) == round(9825.0 * 53 / 54)

    def test_a_closed_order_protects_nothing(self):
        # NBIS's take-profit EXPIRED at the close while still appearing in the order list.
        for status in ("filled", "canceled", "expired", "rejected"):
            found = unprotected_positions([self._pos()], [self._stop(status=status)])
            assert len(found) == 1, status

    def test_a_partially_filled_stop_covers_only_what_remains(self):
        found = unprotected_positions([self._pos()], [self._stop(qty=54, filled=20, status="partially_filled")])
        assert len(found) == 1 and found[0].uncovered == 20.0

    def test_a_BUY_order_does_not_protect_a_LONG(self):
        found = unprotected_positions([self._pos()], [self._stop(side="buy")])
        assert len(found) == 1

    def test_a_SHORT_is_protected_by_a_BUY_stop(self):
        short = self._pos(qty=-54.0)
        assert unprotected_positions([short], [self._stop(side="buy")]) == []
        assert len(unprotected_positions([short], [self._stop(side="sell")])) == 1

    def test_a_flat_position_is_not_naked(self):
        assert unprotected_positions([self._pos(qty=0)], []) == []

    def test_coverage_is_judged_per_instrument_across_strategies(self):
        # The broker does not know about our sleeves. One stop on the instrument covers both legs.
        legs = [self._pos(strat="MANUAL-001", qty=2, mv=364), self._pos(strat="MOMENTUM-002", qty=54, mv=9825)]
        assert unprotected_positions(legs, [self._stop(qty=56)]) == []
        # And a stop too small for the combined size leaves both legs partly exposed.
        partial = unprotected_positions(legs, [self._stop(qty=28)])
        assert len(partial) == 2
        assert round(sum(x.uncovered for x in partial), 6) == 28.0

    def test_the_real_book_audits_to_eleven_naked(self):
        # 2026-08-13: 12 positions, only NBIS carried a resting protective sell.
        held = ["AEM", "BDX", "CGAU", "FIG", "FSM", "NBIS", "OKTA", "SMH", "VCTR", "VFLO", "WHD", "WPM"]
        positions = [self._pos(iid=f"{s}.X", qty=10, mv=1000) for s in held]
        naked = unprotected_positions(positions, [self._stop(sym="NBIS.X", qty=10)])
        assert len(naked) == 11
        assert "NBIS.X" not in {n.instrument_id for n in naked}
        assert round(sum(n.uncovered_notional for n in naked)) == 11000


class TestPlaceability:
    """Lost once already: this class sat after TestUnprotectedAudit and was truncated when that class was
    rewritten. The ceiling injection then passed cleanly, which is exactly the silent-coverage-loss this
    whole module is about."""

    def test_a_ceiling_clamped_width_is_NOT_placeable(self):
        # A ceiling clamp means the symbol's normal movement is larger than policy allows us to sit
        # through, so the order would protect less than one ATR and give false comfort. Placement must
        # refuse it rather than quietly under-protect. (codex review, High.)
        w = trail_width(248.53, 27.78, multiple=1.5, max_pct=15.0)  # NBIS wants 16.8%
        assert w is not None and w.clamped == "ceiling"
        assert w.placeable is False

    def test_a_floor_clamped_width_IS_placeable(self):
        # Merely wider than the symbol strictly needs — safe, just conservative.
        w = trail_width(100, 0.2, multiple=1.5, min_pct=1.0)
        assert w is not None and w.clamped == "floor" and w.placeable is True

    def test_an_ordinary_width_is_placeable(self):
        w = trail_width(181.95, 6.94, multiple=1.5)
        assert w is not None and w.placeable is True


class TestRealDtoShapes:
    """The audit meets two different vocabularies, and getting either wrong makes it lie."""

    def test_a_SHORT_is_read_from_the_side_field_not_the_sign(self):
        # Our PositionDTO reports side=SHORT with a POSITIVE quantity. Inferring direction from the sign
        # alone read every short as long, so a short with a valid BUY stop was reported naked while a
        # SELL stop counted as protection. (codex review, High.)
        short = {"instrument_id": "AEM.XNYS", "side": "SHORT", "quantity": 54.0,
                 "market_value": 9825.0, "strategy_id": "MANUAL-001"}
        buy_stop = {"symbol": "AEM.XNYS", "qty": 54, "type": "stop", "side": "buy", "status": "new"}
        sell_stop = {"symbol": "AEM.XNYS", "qty": 54, "type": "stop", "side": "sell", "status": "new"}
        assert unprotected_positions([short], [buy_stop]) == []
        assert len(unprotected_positions([short], [sell_stop])) == 1

    def test_an_engine_frame_status_counts_as_open(self):
        # Our engine emits Nautilus's `order.status.name` in upper case; Alpaca's REST returns lowercase.
        # Knowing only one vocabulary would ignore a live protective stop from the other.
        pos = {"instrument_id": "AEM.XNYS", "side": "LONG", "quantity": 54.0, "market_value": 9825.0}
        for status in ("ACCEPTED", "SUBMITTED", "new", "partially_filled"):
            order = {"symbol": "AEM.XNYS", "qty": 54, "type": "TRAILING_STOP_MARKET",
                     "side": "sell", "status": status}
            assert unprotected_positions([pos], [order]) == [], status

    def test_a_dotted_ticker_is_not_matched_by_its_prefix(self):
        # `BRK.B.XNYS` must not be protected by a stop on `BRK`. Splitting on the FIRST dot did exactly
        # that. (codex review, High.)
        pos = {"instrument_id": "BRK.B.XNYS", "side": "LONG", "quantity": 10.0, "market_value": 5000.0}
        wrong = {"symbol": "BRK", "qty": 10, "type": "stop", "side": "sell", "status": "new"}
        right = {"symbol": "BRK.B", "qty": 10, "type": "stop", "side": "sell", "status": "new"}
        assert len(unprotected_positions([pos], [wrong])) == 1
        assert unprotected_positions([pos], [right]) == []

    def test_opposite_side_legs_do_not_net_each_other_away(self):
        # Two sleeves on the same name, opposite sides, netting to zero would make BOTH vanish from the
        # audit — and neither is protected. They also need stops on different sides, so coverage cannot
        # be pooled. (codex review, High.)
        legs = [
            {"instrument_id": "AEM.XNYS", "side": "LONG", "quantity": 54.0, "market_value": 9825.0,
             "strategy_id": "MOMENTUM-002"},
            {"instrument_id": "AEM.XNYS", "side": "SHORT", "quantity": 54.0, "market_value": 9825.0,
             "strategy_id": "MANUAL-001"},
        ]
        naked = unprotected_positions(legs, [])
        assert len(naked) == 2, "neither leg is protected; netting must not hide them"

        # A SELL stop covers only the long leg.
        sell_stop = {"symbol": "AEM.XNYS", "qty": 54, "type": "stop", "side": "sell", "status": "new"}
        still_naked = unprotected_positions(legs, [sell_stop])
        assert len(still_naked) == 1 and still_naked[0].strategy_id == "MANUAL-001"


# --- #239: deciding WHICH stops to place, and refusing rather than under-protecting ------------------


def _pos(symbol="NBIS.XNAS", qty=100.0, side="LONG", mv=10000.0, strategy="MOMENTUM-002"):
    return {"instrument_id": symbol, "quantity": qty, "side": side,
            "market_value": mv, "strategy_id": strategy}


def _order(symbol="NBIS", side="sell", qty=100.0, otype="trailing_stop", status="new"):
    return {"symbol": symbol, "side": side, "qty": qty, "order_type": otype,
            "status": status, "filled_qty": 0.0}


def test_a_naked_position_yields_a_stop_for_its_whole_quantity():
    plan = plan_protection(
        positions=[_pos(qty=100.0)],
        orders=[],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert len(plan.intents) == 1
    assert plan.intents[0].quantity == 100.0
    assert plan.intents[0].side == "SELL"          # reduces a long
    assert plan.intents[0].trail_bps == 750        # 5/100 = 5% ATR x 1.5 = 7.5% = 750bps
    assert plan.refusals == []


def test_a_take_profit_limit_does_NOT_count_as_protection():
    """The live case on 2026-08-14. The whole book had three resting orders and every one was a limit sell
    ABOVE the market — OKTA 66 @161.60, FIG 266 @27.80, WDAY 21 @223.98. A limit above the market does
    nothing on the way down, so all three positions were naked while appearing to have orders.

    NBIS carried exactly this shape on 2026-08-12: a stop AND a limit, and the limit outlived the stop.
    """
    plan = plan_protection(
        positions=[_pos(qty=100.0)],
        orders=[_order(otype="limit", qty=100.0)],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    # Still NOT counted as coverage — that invariant is the point of this test and it holds.
    assert plan.covered_instrument_ids == set()
    # But the shares it reserves cannot carry a stop either (#287), so the honest outcome is a reported
    # refusal rather than an order the venue rejects. Before #287 this asserted a 100-share intent, which
    # is exactly what earned `403 insufficient qty available` on the live book.
    assert plan.intents == []
    assert [r.reason for r in plan.refusals] == ["reserved_by_other_order"]


def test_an_existing_protective_stop_suppresses_the_position_entirely():
    plan = plan_protection(
        positions=[_pos(qty=100.0)],
        orders=[_order(otype="trailing_stop", qty=100.0)],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert plan.intents == []
    assert plan.refusals == []


def test_partial_coverage_places_only_the_shortfall_never_the_whole_position():
    """A 1-share stop on 100 shares leaves 99 exposed. Placing a fresh 100 on top would put 101 shares of
    resting sells against a 100-share position — the oversell that #245 produced."""
    plan = plan_protection(
        positions=[_pos(qty=100.0)],
        orders=[_order(otype="stop", qty=40.0)],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert len(plan.intents) == 1
    assert plan.intents[0].quantity == 60.0


def test_a_ceiling_clamp_REFUSES_rather_than_placing_an_under_protective_stop():
    """NBIS at 1.5x ATR wants a 16.8% trail, past the 15% ceiling. Clamping and placing anyway rests an
    order that protects less than one ATR and reads as protection — false comfort is worse than a visible
    gap. The refusal is REPORTED, so `$X unprotected` stays honest."""
    plan = plan_protection(
        positions=[_pos(qty=100.0, mv=10000.0)],
        atr_by_symbol={"NBIS.XNAS": 11.18},        # 11.18% ATR x 1.5 = 16.8% > 15% ceiling
        price_by_symbol={"NBIS.XNAS": 100.0},
        orders=[],
    )
    assert plan.intents == []
    assert len(plan.refusals) == 1
    assert plan.refusals[0].reason == "ceiling"
    assert plan.refusals[0].uncovered_notional == 10000.0


def test_a_symbol_with_no_ATR_is_refused_and_COUNTED_not_silently_skipped():
    """"Do not guess" is right — a symbol we cannot measure gets no arbitrary stop. But a position dropped
    from the plan without appearing in the refusals is a position the audit reports as handled.

    A term classified as an exception does not appear in the accounting, so its exposure lands in whichever
    number is left — here, in "protected". (kumo-lab, 2026-08-14.)
    """
    plan = plan_protection(
        positions=[_pos(qty=100.0, mv=10000.0)],
        orders=[],
        atr_by_symbol={},                            # no history for this symbol
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert plan.intents == []
    assert [r.reason for r in plan.refusals] == ["no_atr"]
    assert plan.uncovered_notional == 10000.0        # still counted as exposed


def test_a_SHORT_position_is_protected_by_a_BUY_stop():
    plan = plan_protection(
        positions=[_pos(qty=100.0, side="SHORT")],
        orders=[],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert len(plan.intents) == 1
    assert plan.intents[0].side == "BUY"


def test_every_position_appears_in_exactly_one_of_intents_or_refusals():
    """The accounting invariant. A position that is neither protected, nor planned, nor refused has silently
    left the audit — which is how "the book is covered" becomes true by omission."""
    positions = [
        _pos("A.XNAS", 100.0, mv=1000.0),                      # naked, measurable -> intent
        _pos("B.XNAS", 100.0, mv=2000.0),                      # no ATR            -> refusal
        _pos("C.XNAS", 100.0, mv=3000.0),                      # ceiling clamp     -> refusal
        _pos("D.XNAS", 100.0, mv=4000.0),                      # already covered   -> neither
    ]
    plan = plan_protection(
        positions=positions,
        orders=[_order(symbol="D", otype="trailing_stop", qty=100.0)],
        atr_by_symbol={"A.XNAS": 5.0, "C.XNAS": 11.18, "D.XNAS": 5.0},
        price_by_symbol={s["instrument_id"]: 100.0 for s in positions},
    )
    accounted = {i.instrument_id for i in plan.intents} | {r.instrument_id for r in plan.refusals}
    assert accounted == {"A.XNAS", "B.XNAS", "C.XNAS"}
    assert plan.covered_instrument_ids == {"D.XNAS"}
    # 2000 (no ATR) + 3000 (ceiling) — NOT A, which is about to be protected.
    assert plan.uncovered_notional == 5000.0


def test_one_instrument_held_by_TWO_strategies_covers_BOTH_and_drops_NEITHER():
    """The #273 exhaustiveness invariant, kept — while the shape it used to assert is REVERSED (#748).

    WHAT THIS TEST WAS RIGHT ABOUT, AND STILL IS. AEM held 2 by MANUAL-001 and 54 by MOMENTUM-002.
    `unprotected_positions` returns a row per strategy, and keying those rows by instrument alone
    silently discarded one: the plan covered 54 of 56 and the missing 2 appeared in neither `intents`
    nor `refusals`, so the audit reported them protected while they were naked. Nothing may be
    dropped. That is asserted below, on the TOTAL, and it is the durable half.

    WHAT IT WAS WRONG ABOUT. It went on to require ONE aggregated stop, on the grounds that "two stops
    totalling 56 on a 56-share position would both stay independently valid at Alpaca, which is the
    oversell #245 produced". Two stops totalling EXACTLY 56 against 56 held is exact coverage, not an
    oversell — #245 was 101 against 100, which is OVER-coverage. The arithmetic was wrong, and it was
    load-bearing: it is why the lane was discarded, and discarding the lane is the phantom mint (#748).

    MEASURED, NOT ARGUED. The live paper book currently rests two independently valid stops summing to
    precisely the held quantity on six symbols — AEM 9+9=18, DELL 7+2=9, GMAB 59+59=118, HALO 19+18=37,
    RGEN 11+11=22, SSRM 53+51=104 — across 329 sells with ZERO rejections and no oversell. The venue
    has been holding this shape for weeks. A comment that reads as a safety property while the venue
    says otherwise is the most dangerous kind of wrong, so it is reversed here rather than left.
    """
    positions = [
        {"instrument_id": "AEM.XNYS", "quantity": 2, "side": "LONG",
         "market_value": 400.0, "strategy_id": "MANUAL-001"},
        {"instrument_id": "AEM.XNYS", "quantity": 54, "side": "LONG",
         "market_value": 10000.0, "strategy_id": "MOMENTUM-002"},
    ]
    plan = plan_protection(
        positions=positions, orders=[],
        atr_by_symbol={"AEM.XNYS": 5.0}, price_by_symbol={"AEM.XNYS": 100.0},
    )
    # EXHAUSTIVE: every held share is accounted for. This is the #273 invariant and it does not
    # depend on how many orders carry it.
    assert sum(i.quantity for i in plan.intents) == 56.0, "a share fell out of the audit"
    assert plan.refusals == []
    # ONE STOP PER HOLDER, each owned by the lane whose shares it covers, so a fill resolves to
    # `{instrument}-{that lane}` instead of to a position that never existed.
    assert {i.strategy_id: i.quantity for i in plan.intents} == {
        "MANUAL-001": 2.0, "MOMENTUM-002": 54.0,
    }


def test_opposite_side_legs_on_one_instrument_get_a_stop_EACH_on_their_own_side():
    """The mirror case, and why the key is (instrument, reducing side) rather than instrument alone.

    Two sleeves on opposite sides of one name each need a stop on their own reducing side, and their
    coverage cannot be pooled — a SELL stop does nothing for a short.
    """
    positions = [
        {"instrument_id": "AEM.XNYS", "quantity": 50, "side": "LONG",
         "market_value": 5000.0, "strategy_id": "MANUAL-001"},
        {"instrument_id": "AEM.XNYS", "quantity": 30, "side": "SHORT",
         "market_value": -3000.0, "strategy_id": "MOMENTUM-002"},
    ]
    plan = plan_protection(
        positions=positions, orders=[],
        atr_by_symbol={"AEM.XNYS": 5.0}, price_by_symbol={"AEM.XNYS": 100.0},
    )
    assert sorted((i.side, i.quantity) for i in plan.intents) == [("BUY", 30.0), ("SELL", 50.0)]


def test_no_held_share_escapes_the_plan_when_strategies_share_an_instrument():
    """The invariant stated in ProtectionPlan's docstring, asserted on quantity rather than on membership.

    Membership alone passed while 2 of 56 shares were missing — the instrument WAS in `intents`, just for
    the wrong amount. Counting shares is what catches that.
    """
    positions = [
        {"instrument_id": "X.XNAS", "quantity": q, "side": "LONG",
         "market_value": q * 100.0, "strategy_id": f"S-{i}"}
        for i, q in enumerate([3, 11, 42])
    ]
    plan = plan_protection(
        positions=positions, orders=[],
        atr_by_symbol={"X.XNAS": 5.0}, price_by_symbol={"X.XNAS": 100.0},
    )
    assert sum(i.quantity for i in plan.intents) == 56.0


class TestBrokerRows:
    def _p(self, symbol="AEM", qty="54", mv="9825.0", side="long"):
        return {"symbol": symbol, "qty": qty, "market_value": mv, "side": side}

    def test_maps_a_long_to_the_row_shape_plan_protection_reads(self):
        rows, unresolved = broker_rows([self._p()], lambda s: f"{s}.XNYS")
        assert unresolved == []
        assert rows == [{"instrument_id": "AEM.XNYS", "quantity": 54.0, "side": "LONG",
                         "market_value": 9825.0, "strategy_id": ""}]

    def test_an_UNRESOLVABLE_symbol_is_returned_not_dropped(self):
        """A position we cannot map still holds exposure. Dropping it silently removes that exposure from
        the audit, which is the same failure `Refusal` exists to prevent one step later."""
        rows, unresolved = broker_rows([self._p(symbol="WEIRD")], lambda s: None)
        assert rows == []
        assert unresolved == ["WEIRD"]

    def test_a_short_is_carried_as_SHORT_from_the_side_field(self):
        rows, _ = broker_rows([self._p(qty="-30", side="short")], lambda s: f"{s}.XNYS")
        assert rows[0]["side"] == "SHORT" and rows[0]["quantity"] == 30.0

    def test_falls_back_to_the_SIGN_when_side_is_absent(self):
        rows, _ = broker_rows([{"symbol": "AEM", "qty": "-30", "market_value": "-3000"}], lambda s: f"{s}.XNYS")
        assert rows[0]["side"] == "SHORT"

    def test_unparseable_numbers_are_reported_rather_than_defaulted_to_zero(self):
        """A quantity of 0 from a bad parse reads as 'flat', and a flat position is skipped entirely — so a
        parse failure would silently mark a real position as needing nothing."""
        rows, unresolved = broker_rows([self._p(qty="not-a-number")], lambda s: f"{s}.XNYS")
        assert rows == [] and unresolved == ["AEM"]


# --- #169 / codex Critical 5: coverage that EXCEEDS the position ------------------------------------


def test_coverage_larger_than_the_position_is_reported_as_oversize():
    """codex, Critical. The reconciler only ever ADDED coverage, so an oversized stop persisted forever.

    A position can shrink between the broker read and the submit — or a trim can fill after the stop was
    sized — leaving a GTC stop for more shares than are held. When it triggers it sells shares that are not
    there: the position over-liquidates and FLIPS, opening opposite exposure nobody asked for.
    """
    plan = plan_protection(
        positions=[_pos(qty=60.0)],
        orders=[_order(otype="trailing_stop", qty=100.0)],   # sized for a position since trimmed to 60
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert plan.intents == []                    # already covered — nothing to add
    assert len(plan.oversize) == 1
    over = plan.oversize[0]
    assert over.instrument_id == "NBIS.XNAS"
    assert over.held == 60.0
    assert over.covered == 100.0
    assert over.target_quantity == 60.0          # shrink to the live position, not cancel outright


def test_exact_coverage_is_not_reported_as_oversize():
    plan = plan_protection(
        positions=[_pos(qty=100.0)],
        orders=[_order(otype="trailing_stop", qty=100.0)],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert plan.oversize == []


def test_a_take_profit_limit_is_not_counted_when_judging_OVERSIZE_either():
    """The same type filter both ways. A 100-share take-profit over a 60-share position is not oversize
    protection — it is not protection at all, and shrinking it would be meddling with a target."""
    plan = plan_protection(
        positions=[_pos(qty=60.0)],
        orders=[_order(otype="limit", qty=100.0)],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert plan.oversize == []                   # a limit is not oversize PROTECTION, it is not protection
    # The position is still naked, and now says so as a refusal rather than as a doomed order (#287).
    assert plan.intents == []
    assert [r.reason for r in plan.refusals] == ["reserved_by_other_order"]

def test_oversize_names_the_LARGEST_protective_order_to_shrink():
    """With several protective orders on one leg, the excess comes off the biggest — shrinking a small one
    to a negative quantity is meaningless, and cancelling outright would open a naked window."""
    plan = plan_protection(
        positions=[_pos(qty=50.0)],
        orders=[
            _order(otype="trailing_stop", qty=20.0),
            _order(otype="stop", qty=80.0),
        ],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert len(plan.oversize) == 1
    over = plan.oversize[0]
    assert over.covered == 100.0 and over.held == 50.0
    assert over.order["qty"] == 80.0             # the larger one
    assert over.target_quantity == 30.0          # 80 - 50 excess, leaving 20 + 30 = 50


def test_a_position_that_closed_entirely_still_reports_its_orphan_stop():
    """Flat with a stop still resting is the worst version: the stop has no position behind it at all, so
    triggering it opens a naked SHORT. The position is gone from `positions`, so this must be found from
    the ORDERS side rather than by iterating positions."""
    plan = plan_protection(
        positions=[],
        orders=[_order(otype="trailing_stop", qty=100.0)],
        atr_by_symbol={},
        price_by_symbol={},
    )
    assert len(plan.oversize) == 1
    assert plan.oversize[0].held == 0.0
    assert plan.oversize[0].target_quantity == 0.0   # 0 means cancel — there is nothing to protect


def test_the_excess_is_taken_across_AS_MANY_ORDERS_AS_IT_TAKES():
    """codex, Critical. Correcting only the largest order leaves the leg over-covered.

    Held 50 against stops of 40 + 40 + 40. The excess is 70, the largest order is 40, so shrinking just
    that one clamps at 0 and leaves 80 covering 50 — still over-covered by 30, still able to
    over-liquidate and flip the position. The previous test passed only because its largest order (80)
    happened to exceed its excess (50).

    Corrections must run largest-first until aggregate coverage equals what is held.
    """
    plan = plan_protection(
        positions=[_pos(qty=50.0)],
        orders=[
            {**_order(otype="trailing_stop", qty=40.0), "id": "v1"},
            {**_order(otype="stop", qty=40.0), "id": "v2"},
            {**_order(otype="stop_limit", qty=40.0), "id": "v3"},
        ],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    # Each physical order is corrected AT MOST ONCE, keyed by the VENUE id. Asserting on Python `id()`
    # proves nothing: `protective_orders` builds a fresh dict per input, so identities always differ —
    # my first attempt at this check passed with the bug fully present.
    touched = [str(o.order.get("id") or o.order.get("client_order_id") or "") for o in plan.oversize]
    assert len(touched) == len(set(touched))

    # What remains resting once every correction is applied must equal what is held. Stated as the total,
    # because per-order assertions pass while the aggregate is still wrong — which is how the
    # correct-only-the-largest bug survived its first test.
    corrected = {id(o.order): o.target_quantity for o in plan.oversize}
    every_order = [
        {"_remaining": 40.0}, {"_remaining": 40.0}, {"_remaining": 40.0},
    ]
    resting_after = sum(
        corrected.get(id(o.order), o.order["_remaining"]) for o in plan.oversize
    ) + sum(
        r["_remaining"] for i, r in enumerate(every_order) if i >= len(plan.oversize)
    )
    assert resting_after == 50.0


def test_one_venue_order_repeated_in_the_payload_is_counted_ONCE():
    """A broker payload that lists the same order twice must not read as double the coverage.

    Inflated coverage makes a correctly-sized position look over-covered, and the "correction" then shrinks
    a real stop for no reason — manufacturing the naked gap it exists to close.
    """
    same = {"id": "venue-1", "symbol": "NBIS", "side": "sell", "qty": 100.0,
            "order_type": "trailing_stop", "status": "new", "filled_qty": 0.0}
    plan = plan_protection(
        positions=[_pos(qty=100.0)],
        orders=[same, dict(same)],                 # same venue order, listed twice
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert plan.oversize == []                     # 100 covering 100 — exactly right, nothing to correct
    assert plan.intents == []


def test_a_flat_position_cancels_EVERY_protective_order_on_the_leg():
    """codex, Critical: with nothing held, one cancellation is not enough — every orphan stop must go.
    Each is independently able to open a naked short out of nothing."""
    plan = plan_protection(
        positions=[],
        orders=[
            _order(otype="trailing_stop", qty=40.0),
            _order(otype="stop", qty=60.0),
        ],
        atr_by_symbol={},
        price_by_symbol={},
    )
    assert len(plan.oversize) == 2
    assert all(o.target_quantity == 0.0 for o in plan.oversize)


# --- #288: PEAK's trail widths must scale with the symbol, like every other stop we place -------------


def test_peak_widths_scale_with_the_symbol_not_a_fixed_percentage():
    """#288, found on the live book 2026-08-14.

    FIG carried a PEAK trailing stop at a flat 2.5% against an ATR of 7.94% — 0.31x ATR, which fires on an
    ordinary session rather than on anything going wrong. The same 2.5% is 1.52x ATR on VFLO.

    This is the argument #239 already settled for backstop stops, in a system that was never revisited:
    a single percentage cannot serve a book whose ATR spans 1.64% to 11.18%.
    """
    fig = peak_trail_bps(price=26.28, atr_value=2.087, wide_multiple=1.5, tight_multiple=0.75)
    vflo = peak_trail_bps(price=53.42, atr_value=0.88, wide_multiple=1.5, tight_multiple=0.75)
    assert fig is not None and vflo is not None

    # FIG's ATR is ~4.8x VFLO's, so its stop must be far wider — not identical.
    assert fig.wide_bps > vflo.wide_bps * 3
    # And the tight leg stays tighter than the wide one, which is the whole point of the blowoff branch.
    assert fig.tight_bps < fig.wide_bps
    assert vflo.tight_bps < vflo.wide_bps


def test_peak_widths_reuse_the_SAME_derivation_as_the_backstop():
    """One derivation of "how wide should a stop on this symbol be", not two.

    Two will disagree, and here they already did by a factor of five — #239 computed 11.91% for FIG while
    PEAK rested 2.5%. Two systems placing trailing stops on one instrument with widths differing 5x means
    whichever rests last wins and the reconciler reads the other as coverage.
    """
    price, atr_value = 26.28, 2.087
    peak = peak_trail_bps(price=price, atr_value=atr_value, wide_multiple=1.5, tight_multiple=0.75)
    backstop = trail_width(price, atr_value, multiple=1.5)
    assert peak is not None and backstop is not None
    assert peak.wide_bps == backstop.bps


def test_peak_widths_honour_the_floor_so_a_quiet_name_is_not_stopped_on_noise():
    quiet = peak_trail_bps(price=100.0, atr_value=0.2, wide_multiple=1.5, tight_multiple=0.75, min_pct=1.0)
    assert quiet is not None
    assert quiet.wide_bps == 100          # floored at 1.0%
    assert quiet.tight_bps >= 10          # never below Alpaca's own 0.1% minimum


def test_peak_widths_REFUSE_on_a_ceiling_clamp_rather_than_under_protecting():
    """Same rule as the backstop: past the ceiling the order protects less than one ATR, and a stop that
    gives false comfort is worse than a visible refusal."""
    violent = peak_trail_bps(price=248.53, atr_value=27.78, wide_multiple=1.5, tight_multiple=0.75)
    assert violent is None


def test_peak_widths_return_None_rather_than_guessing_without_an_ATR():
    assert peak_trail_bps(price=100.0, atr_value=0.0, wide_multiple=1.5, tight_multiple=0.75) is None
    assert peak_trail_bps(price=0.0, atr_value=5.0, wide_multiple=1.5, tight_multiple=0.75) is None


def test_the_FIG_case_that_prompted_this_now_produces_a_survivable_width():
    """The live number, pinned so a future edit that reintroduces a fixed percentage fails here.

    FIG on 2026-08-14: price 26.28, ATR 2.087 (7.94%). PEAK rested 2.5% — 0.31x ATR — which fires on an
    ordinary session. The whole point is that this now lands near the backstop's own 1.5x.
    """
    w = peak_trail_bps(price=26.28, atr_value=2.087, wide_multiple=1.5, tight_multiple=0.75)
    assert w is not None
    assert round(w.atr_pct, 2) == 7.94
    assert w.wide_bps == 1191            # 11.91% — not 250
    assert w.wide_bps > 250 * 4          # emphatically not the flat 2.5% that prompted #288


def test_the_tight_leg_stays_below_the_wide_one_even_when_the_FLOOR_lifts_both():
    """The blowoff branch exists to TIGHTEN. On a quiet symbol the 1.0% floor lifts both legs to the same
    width, and a tighten that changes nothing is a branch that silently does not work.

    Removing the step-below passed every other test here — the two legs were only ever compared on symbols
    volatile enough for the floor not to bind.
    """
    quiet = peak_trail_bps(price=100.0, atr_value=0.2, wide_multiple=1.5, tight_multiple=0.75, min_pct=1.0)
    assert quiet is not None
    assert quiet.wide_bps == 100            # both want 1.0% before the step
    assert quiet.tight_bps < quiet.wide_bps
    assert quiet.tight_bps >= 10            # never under Alpaca's own 0.1% minimum


# --- #287: shares reserved by a NON-protective resting sell cannot carry a stop ----------------------


def test_shares_locked_behind_a_take_profit_cannot_take_a_stop_and_are_reported():
    """Found the moment #239 was switched on, 2026-08-13 15:45 ET.

        PROT-SELL-OKTA: 403 insufficient qty available (requested: 68, available: 2)

    OKTA held 68 with a resting take-profit LIMIT for 66. `PROTECTIVE_TYPES` rightly excludes that limit
    from COVERAGE — it sits above the market and does nothing on the way down — but Alpaca reserves shares
    against ANY resting sell. So both are true at once: the 68 shares are unprotected, and 66 of them are
    unavailable. Requesting the full 68 is rejected outright, and the position gets NOTHING.

    Place what can be placed, and report the rest. Two of OKTA's shares can carry a stop; the other 66
    genuinely cannot while that limit rests, and saying so keeps `uncovered_notional` honest.
    """
    plan = plan_protection(
        positions=[_pos(qty=68.0, mv=10200.0)],
        orders=[_order(otype="limit", qty=66.0)],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert len(plan.intents) == 1
    assert plan.intents[0].quantity == 2.0
    assert [r.reason for r in plan.refusals] == ["reserved_by_other_order"]
    # 66 of 68 shares, at their share of market value.
    assert round(plan.refusals[0].uncovered_notional) == round(10200.0 * 66 / 68)


def test_a_fully_reserved_position_places_NOTHING_rather_than_a_doomed_order():
    """PBF: held 134, take-profit for 134, available 0. Submitting anyway earns a 403 and, because the
    in-flight marker expires by design, retries it every three ticks for as long as the limit rests."""
    plan = plan_protection(
        positions=[_pos(qty=134.0, mv=9900.0)],
        orders=[_order(otype="limit", qty=134.0)],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert plan.intents == []
    assert [r.reason for r in plan.refusals] == ["reserved_by_other_order"]
    assert round(plan.refusals[0].uncovered_notional) == 9900


def test_a_protective_stop_reserves_shares_too_but_those_are_already_covered():
    """The reservation is real for protective orders as well — it just coincides with coverage there, so
    the shortfall arithmetic must not double-count it. 40 covered of 100 leaves 60 both uncovered AND
    available."""
    plan = plan_protection(
        positions=[_pos(qty=100.0)],
        orders=[_order(otype="stop", qty=40.0)],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert len(plan.intents) == 1
    assert plan.intents[0].quantity == 60.0
    assert plan.refusals == []


def test_a_take_profit_and_a_partial_stop_together_leave_only_what_is_free():
    """Held 100, a 30-share protective stop, a 50-share take-profit. Uncovered is 70, but only 20 shares
    are unreserved — so 20 is what can be placed and 50 must be reported."""
    plan = plan_protection(
        positions=[_pos(qty=100.0, mv=10000.0)],
        orders=[_order(otype="stop", qty=30.0), _order(otype="limit", qty=50.0)],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert len(plan.intents) == 1
    assert plan.intents[0].quantity == 20.0
    assert [r.reason for r in plan.refusals] == ["reserved_by_other_order"]
    assert round(plan.refusals[0].uncovered_notional) == 5000


def test_peak_widths_REFUSE_when_the_floor_collapses_the_two_legs_together():
    """codex, Medium. `minTrailPct` bottoms out at 0.1 in the schema, so this is reachable from settings.

    At min_pct=0.1 a quiet symbol floors `wide` at 10bps; the step-below gives 9, and the 10bps venue
    minimum lifts it back to 10 — tight == wide, and the blowoff branch silently becomes a no-op. A
    manager whose tighten does nothing is worse than one that refuses to arm, because nothing reports it.
    """
    collapsed = peak_trail_bps(
        price=100.0, atr_value=0.001, wide_multiple=1.5, tight_multiple=0.75, min_pct=0.1,
    )
    assert collapsed is None

    # One notch above the floor the legs separate again and the arm proceeds.
    ok = peak_trail_bps(price=100.0, atr_value=0.5, wide_multiple=1.5, tight_multiple=0.75, min_pct=0.1)
    assert ok is not None and ok.tight_bps < ok.wide_bps


def test_TWO_LANES_on_one_symbol_get_ONE_STOP_EACH_stamped_with_the_holder():
    """The mint (#748). Aggregating across sleeves is what makes a protective fill a PHANTOM.

    THE MECHANISM, measured rather than argued. Under NETTING the execution engine derives the
    position a fill belongs to as `PositionId(f"{fill.instrument_id}-{fill.strategy_id}")`
    (`execution/engine.pyx`), and `fill.strategy_id` comes from the ORDER — on the adapter path
    (`execution/client.pyx:881`) and, the one that matters on paper, on the reconciliation path
    (`live/reconciliation.py:410,526`, `strategy_id=order.strategy_id`), because this account has no
    trade-updates socket and reconciliation IS the fill path. A stop built by the display strategy's
    own `order_factory` therefore resolves to `{instrument}-MANUAL-001` — a position that has never
    existed — so a reduce-only fill is applied to NO position and the poll fabricates the difference.

    WHY THE AGGREGATION WAS THERE, AND WHY IT IS WRONG. The comment above `legs` says emitting one
    intent per row "rests two stops totalling 56 against a 56-share position ... that is the oversell
    of #245". Two stops totalling EXACTLY 56 against 56 held is exact coverage, not an oversell; #245
    was 101 against 100, which is over-coverage. The arithmetic in that comment is wrong, and the live
    book refutes it: six symbols currently rest two independently valid stops summing to precisely the
    held quantity (AEM 9+9=18, SSRM 53+51=104, GMAB 59+59=118, ...) across 329 sells with 0 rejections.

    THE TOTAL DOES NOT CHANGE. The sum of the per-lane shortfalls is the combined shortfall placed
    today. What changes is that it arrives as one exactly-sized stop per holder, each owned by the
    lane whose shares it covers.
    """
    positions = [
        _pos(qty=54.0, strategy="MOMENTUM-002", mv=5400.0),
        _pos(qty=2.0, strategy="MANUAL-001", mv=200.0),
    ]
    # FIXTURE PROPERTY FIRST. The fixture must actually present two DIFFERENT holders of ONE
    # instrument, or the assertion below passes against a rule that does nothing — and this is the
    # exact 54/2 AEM split the aggregation comment itself cites.
    assert len({p["instrument_id"] for p in positions}) == 1, "fixture must be one instrument"
    assert len({p["strategy_id"] for p in positions}) == 2, "fixture must have two distinct lanes"

    plan = plan_protection(
        positions=positions,
        orders=[],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )

    assert len(plan.intents) == 2, (
        f"one stop per holding lane, not one aggregated stop: got {plan.intents}"
    )
    by_lane = {i.strategy_id: i.quantity for i in plan.intents}
    assert by_lane == {"MOMENTUM-002": 54.0, "MANUAL-001": 2.0}, by_lane

    # THE TOTAL IS UNCHANGED — this is what makes the split safe rather than an over-coverage. If this
    # ever stops holding, the split has become the #245 shape for real.
    assert sum(i.quantity for i in plan.intents) == 56.0

    # Every intent names its owner. An unstamped intent is the defect wearing a different shape: the
    # order would still be built by whichever strategy happens to submit it.
    assert all(i.strategy_id for i in plan.intents), plan.intents


def test_a_SINGLE_lane_still_yields_exactly_one_stop():
    """The split must not multiply stops where there is only one holder.

    Aim at the class: the risk of keying by lane is that it silently changes the common case too —
    every single-holder symbol on the book is the overwhelming majority, and doubling their stops
    would be an oversell the venue WOULD punish.
    """
    plan = plan_protection(
        positions=[_pos(qty=100.0, strategy="MOMENTUM-002")],
        orders=[],
        atr_by_symbol={"NBIS.XNAS": 5.0},
        price_by_symbol={"NBIS.XNAS": 100.0},
    )
    assert len(plan.intents) == 1
    assert plan.intents[0].quantity == 100.0
    assert plan.intents[0].strategy_id == "MOMENTUM-002"
