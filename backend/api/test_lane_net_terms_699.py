"""The assembly of `net(W) = ΔMV − invested(W)` per lane per period (#699 option a).

`/pnl/unrealized-base` already resolves each window's base DAY from the manifest and reads that
day's observation rows. This adds, from the same rows, the lane's MARKET VALUE at the base (Σ signed
qty × mark), and from the engine's `lane_flows` frame the lane's net invested STRICTLY AFTER the base
day, plus the reasons a window is PARTIAL. The tile subtracts: `mv_now − mv_base − invested`.

Three states everywhere: a term is a number, or None (unknown — never zero), or the whole period is
None (no base). "partial" is a named string, so the cell can show it, not a boolean.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from api.pnl_base import lane_net_terms, unrealized_base_by_period
from api.test_pnl_base_endpoint import _Store, _row

TODAY = date(2026, 9, 14)      # a Monday: 1W's base is 09-07, which the 4-day walk-back resolves to 09-04
BASE = "2026-09-04"


def _bases(store):
    return asyncio.run(unrealized_base_by_period(store, today=TODAY))


def _flows(by_day, earliest="2026-08-17", error=None, internal=None):
    return {"by_day": by_day, "internal": internal or {}, "earliest": earliest, "horizon_days": 100, "error": error}


def _coverage(first_observed=None, captured=None):
    return {"first_observed": first_observed or {}, "captured": set(captured or ())}


PAPER_ROWS = {BASE: [
    _row("MOMENTUM-002", "A.XNYS", BASE, 100, 15.0, 20.0),     # MV 2000, the counter-example's book
    _row("TECHIVOL-005", "CRM.XNYS", BASE, 6, 250.0, 259.5),   # MV 1557
    _row("TECHIVOL-005", "DT.XNYS", BASE, 37, 50.0, 51.9),     # MV 1920.3
]}
COV = _coverage(first_observed={"MOMENTUM-002": "2026-08-31", "TECHIVOL-005": "2026-08-31"},
                # 09-07 (Labor Day) is listed as captured HERE so the weekday-gap logic is isolated;
                # `test_a_HOLIDAY_over_reports_as_a_gap_BY_NAME` pins what happens when it is not.
                captured={"2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", BASE, "2026-09-07",
                          "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"})


def test_FIXTURE_the_1W_base_resolves_to_09_04_on_this_Monday():
    b = _bases(_Store(PAPER_ROWS))
    assert b.base_date["1W"] == BASE and b.by_period["1W"] is not None


def test_market_value_at_the_base_is_signed_qty_times_mark_summed_per_lane():
    b = _bases(_Store(PAPER_ROWS))
    assert b.market_value["1W"] == {"MOMENTUM-002": 2000.0, "TECHIVOL-005": 1557.0 + 1920.3}
    assert b.market_value["all"] is None and b.market_value["1D"] is None, "no base, no market value — never zero"


def test_a_SHORT_at_the_base_has_a_NEGATIVE_market_value():
    rows = {BASE: [_row("BCTROT-004", "S.XNYS", BASE, -10, 50.0, 40.0)]}
    assert _bases(_Store(rows)).market_value["1W"] == {"BCTROT-004": -400.0}


def test_ONE_unmarked_leg_makes_the_LANES_market_value_UNKNOWN_not_smaller():
    rows = {BASE: [_row("TECHIVOL-005", "CRM.XNYS", BASE, 6, 250.0, 259.5),
                   _row("TECHIVOL-005", "DT.XNYS", BASE, 37, 50.0, None)]}
    assert _bases(_Store(rows)).market_value["1W"] == {"TECHIVOL-005": None}


def test_the_COUNTER_EXAMPLE_composes_to_ZERO_through_the_served_terms():
    """MV_base 2000 (100 @ mark 20), sold 50 @ 20 on 09-10, mark flat → MV_now 1000.
    net = 1000 − 2000 − (−1000) = 0. `realized + Δunrealized` said 250."""
    b = _bases(_Store(PAPER_ROWS))
    net = lane_net_terms(b, _flows({"MOMENTUM-002": {"2026-09-10": -1000.0}}), coverage=COV, today=TODAY)
    t = net["1W"]["MOMENTUM-002"]
    assert t == {"mv_base": 2000.0, "invested": -1000.0, "partial": None}
    assert 1000.0 - t["mv_base"] - t["invested"] == 0.0


def test_flows_ON_the_base_day_are_inside_MV_base_and_are_NOT_counted_again():
    b = _bases(_Store(PAPER_ROWS))
    net = lane_net_terms(b, _flows({"TECHIVOL-005": {BASE: 500.0, "2026-09-08": 300.0, "2026-09-11": -100.0}}),
                         coverage=COV, today=TODAY)
    assert net["1W"]["TECHIVOL-005"]["invested"] == 200.0


def test_a_lane_BORN_inside_the_window_is_PARTIAL_and_named_with_mv_base_ZERO():
    """First observed 09-11, base day 09-04 captured with the lane absent: it held nothing then
    (captured-and-flat), its whole net is flows-vs-now, and the cell says so."""
    b = _bases(_Store(PAPER_ROWS))
    cov = _coverage(first_observed={**COV["first_observed"], "QC345-003": "2026-09-11"}, captured=COV["captured"])
    net = lane_net_terms(b, _flows({"QC345-003": {"2026-09-11": 2803.8}}), coverage=cov, today=TODAY)
    assert net["1W"]["QC345-003"] == {"mv_base": 0.0, "invested": 2803.8, "partial": "first observed 2026-09-11"}


def test_a_1040_GAP_inside_the_window_is_PARTIAL_and_NAMED_never_bridged():
    """09-09 and 09-10 not captured on paper (#1040). The arithmetic is exact between the two
    observed ends; the interior is unverified and the cell must say which sessions."""
    b = _bases(_Store(PAPER_ROWS))
    cov = _coverage(first_observed=COV["first_observed"],
                    captured=COV["captured"] - {"2026-09-09", "2026-09-10"})
    net = lane_net_terms(b, _flows({}), coverage=cov, today=TODAY)
    assert net["1W"]["MOMENTUM-002"]["partial"] == "not captured: 2026-09-09, 2026-09-10"
    assert net["1W"]["MOMENTUM-002"]["mv_base"] == 2000.0, "named, not nulled — the ends are observed"


def test_weekends_and_TODAY_are_not_gaps():
    """09-05/06 and 09-12/13 are weekends; 09-14 is today and has no close yet."""
    b = _bases(_Store(PAPER_ROWS))
    net = lane_net_terms(b, _flows({}), coverage=COV, today=TODAY)
    assert net["1W"]["MOMENTUM-002"]["partial"] is None


def test_a_HOLIDAY_over_reports_as_a_gap_BY_NAME_the_safe_direction_for_an_unverified_label():
    """Holidays are not modelled (same honesty as `_sessions_since`): Labor Day 09-07 reads as
    "not captured: 2026-09-07". Over-reporting names the date, so a reader sees it was a holiday;
    under-reporting would hide a real #1040 gap that happened to fall on a weekday."""
    b = _bases(_Store(PAPER_ROWS))
    cov = _coverage(first_observed=COV["first_observed"], captured=COV["captured"] - {"2026-09-07"})
    net = lane_net_terms(b, _flows({}), coverage=cov, today=TODAY)
    assert net["1W"]["MOMENTUM-002"]["partial"] == "not captured: 2026-09-07"


def test_NO_flows_frame_or_a_flows_ERROR_leaves_invested_UNKNOWN_never_zero():
    b = _bases(_Store(PAPER_ROWS))
    for flows in (None, _flows({}, error="cache row unreadable")):
        net = _terms(b, flows, coverage=COV, today=TODAY)
        assert net["1W"]["MOMENTUM-002"]["invested"] is None
        assert net["1W"]["MOMENTUM-002"]["mv_base"] == 2000.0


def test_a_cache_that_cannot_VOUCH_back_to_the_base_leaves_invested_UNKNOWN():
    """`earliest` 09-08 is after the base 09-04: fills between could have happened and be gone."""
    b = _bases(_Store(PAPER_ROWS))
    net = lane_net_terms(b, _flows({"MOMENTUM-002": {"2026-09-10": -1.0}}, earliest="2026-09-08"),
                         coverage=COV, today=TODAY)
    assert net["1W"]["MOMENTUM-002"]["invested"] is None
    ok = lane_net_terms(b, _flows({"MOMENTUM-002": {"2026-09-10": -1.0}}, earliest=BASE), coverage=COV, today=TODAY)
    assert ok["1W"]["MOMENTUM-002"]["invested"] == -1.0, "vouching FROM the base day is enough: its fills are in MV_base"


def test_a_period_WITHOUT_a_base_is_NULL_whole():
    b = _bases(_Store(PAPER_ROWS))
    net = lane_net_terms(b, _flows({}), coverage=COV, today=TODAY)
    assert net["all"] is None and net["1D"] is None and net["3M"] is None


def test_a_lane_with_flows_after_the_base_but_NO_row_at_it_gets_a_term_so_the_tile_can_render_it():
    b = _bases(_Store(PAPER_ROWS))
    net = lane_net_terms(b, _flows({"MANUAL-001": {"2026-09-09": 100.0}}), coverage=COV, today=TODAY)
    assert net["1W"]["MANUAL-001"]["mv_base"] == 0.0 and net["1W"]["MANUAL-001"]["invested"] == 100.0


def test_TRANSFER_and_REPAIR_fills_after_the_base_are_NAMED_and_COUNTED_the_window_stays_numeric():
    """A transfer at carry-over price or a repair at basis is inside the lane's net (the identity
    stays closed) and the reader must be able to see it. Both are cockpit-originated with real
    OrderFilled events under the lane's own id, so they do not make the window dirty."""
    b = _bases(_Store(PAPER_ROWS))
    flows = _flows({"TECHIVOL-005": {"2026-09-11": -150.0, "2026-09-13": -2034.0}},
                   internal={"TECHIVOL-005": {BASE: {"transfer": 9}, "2026-09-11": {"transfer": 2},
                                              "2026-09-13": {"repair": 1}}})
    net = _terms(b, flows, coverage=COV, today=TODAY)
    assert net["1W"]["TECHIVOL-005"]["partial"] == "internal fills: transfer 2026-09-11 (2), repair 2026-09-13 (1)"
    assert net["1W"]["TECHIVOL-005"]["invested"] == -2184.0, "named AND counted"


def _kf(lane, day, instrument, side, qty, px, kind, ts_ns=None):
    """`ts_ns` defaults to noon of `day` so a same-day pair is seconds apart unless a test says otherwise."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if ts_ns is None:
        ts_ns = int(datetime.fromisoformat(f"{day}T12:08:00").replace(tzinfo=ZoneInfo("America/New_York")).timestamp() * 1e9)
    return {"lane": lane, "day": day, "instrument": instrument, "side": side, "qty": float(qty), "px": float(px),
            "kind": kind, "ts_ns": int(ts_ns)}


def _flows2(by_day, *, kernel_fills=(), touched=None, internal=None, earliest="2026-08-17", net_qty=None):
    return {"by_day": by_day, "internal": internal or {}, "kernel_fills": list(kernel_fills),
            "touched": touched or {}, "net_qty": net_qty or {}, "earliest": earliest, "horizon_days": 100, "error": None}


AUTO = object()


def _terms(b, flows, *, coverage, today, qty_now=AUTO):
    """`lane_net_terms` with a qty_now CONSISTENT with the fixture by construction (base qty at the 1W
    base + the lane's own net fills after it) unless a test overrides it — so the qty invariant is
    inert in tests about OTHER rules and load-bearing only where a test breaks it deliberately."""
    if qty_now is AUTO:
        from api.realized import own_fill_qty_after, symbol_of

        base_day = b.base_date.get("1W")
        qty_now = {}
        for lane, held in (b.qty_at.get("1W") or {}).items():
            qty_now.setdefault(lane, {}).update(held)
        for lane in set((flows or {}).get("net_qty") or {}) | set(qty_now):
            own = own_fill_qty_after((flows or {}).get("net_qty") or {}, lane, after=base_day or "")
            cur = {symbol_of(i): q for i, q in qty_now.get(lane, {}).items()}
            for sym, q in own.items():
                cur[sym] = cur.get(sym, 0.0) + q
            qty_now[lane] = {f"{sym}.XNYS": q for sym, q in cur.items()}
    return lane_net_terms(b, flows, coverage=coverage, today=today, qty_now=qty_now)


def _now(**by_lane):
    """`qty_now` as the endpoint builds it from the positions plane: {lane: {instrument: signed qty}}."""
    return {lane: dict(v) for lane, v in by_lane.items()}


def test_PAPER_MOMENTUM_LAND_an_UNPAIRED_inferred_fill_refuses_MOMENTUM_and_leaves_BCTROT_numeric():
    """Paper 2026-09-08: the kernel attributed LAND 3×BUY 429 (uuid coids) to MOMENTUM-002 — unpaired
    synthetic cash (+11,760 under #1070). MOMENTUM's window is refused with LAND named; BCTROT-004,
    which never held or traded LAND, keeps its number (#1071 refused it too — the whole point of #1072)."""
    rows = {BASE: [_row("MOMENTUM-002", "A.XNYS", BASE, 100, 15.0, 20.0),
                   _row("BCTROT-004", "B.XNYS", BASE, 10, 1.0, 100.0)]}
    b = _bases(_Store(rows))
    flows = _flows2({"MOMENTUM-002": {"2026-09-08": 12146.0}, "BCTROT-004": {"2026-09-09": -500.0}},
                    kernel_fills=[_kf("MOMENTUM-002", "2026-09-08", "LAND.XNAS", "BUY", 429, 9.44, "inferred"),
                                  _kf("MOMENTUM-002", "2026-09-08", "LAND.XNAS", "BUY", 429, 9.53, "inferred"),
                                  _kf("MOMENTUM-002", "2026-09-08", "LAND.XNAS", "BUY", 429, 9.33, "inferred")],
                    touched={"MOMENTUM-002": {"2026-09-08": ["LAND.XNAS"]}, "BCTROT-004": {"2026-09-09": ["B.XNYS"]}})
    net = _terms(b, flows, coverage=COV, today=TODAY)["1W"]
    assert net["MOMENTUM-002"]["invested"] is None
    assert net["MOMENTUM-002"]["partial"] == "cache cannot vouch for this window: LAND.XNAS inferred 2026-09-08 (3) on MOMENTUM-002"
    assert net["BCTROT-004"]["invested"] == -500.0 and net["BCTROT-004"]["partial"] is None


def test_PAPER_TECHIVOL_GWRE_a_CROSS_LANE_reconciliation_fill_refuses_the_lane_that_HELD_it_and_leaves_MOMENTUM_numeric():
    """Paper 2026-09-08: TECHIVOL's GWRE 14 left its book with no TECHIVOL fill — the kernel booked
    the sell under EXTERNAL. #1072 a refused TECHIVOL because a kernel fill touched a name it held;
    #1072 b refuses it by the QTY INVARIANT (held 14, 0 now, own fills 0) and no longer looks at
    what EXTERNAL did. MOMENTUM, consistent, is numeric. EXTERNAL's own unpaired fill refuses EXTERNAL."""
    rows = {BASE: [_row("TECHIVOL-005", "GWRE.XNYS", BASE, 14, 160.0, 162.45),
                   _row("TECHIVOL-005", "CRM.XNYS", BASE, 6, 250.0, 259.5),
                   _row("MOMENTUM-002", "A.XNYS", BASE, 100, 15.0, 20.0)]}
    b = _bases(_Store(rows))
    flows = _flows2({"TECHIVOL-005": {"2026-09-10": -1162.76}, "MOMENTUM-002": {"2026-09-10": -1000.0},
                     "EXTERNAL": {"2026-09-08": -2274.3}},
                    kernel_fills=[_kf("EXTERNAL", "2026-09-08", "GWRE.XNYS", "SELL", 14, 162.45, "reconciliation")],
                    touched={"EXTERNAL": {"2026-09-08": ["GWRE.XNYS"]}, "TECHIVOL-005": {"2026-09-10": ["CRM.XNYS"]},
                             "MOMENTUM-002": {"2026-09-10": ["A.XNYS"]}})
    now = _now(**{"TECHIVOL-005": {"CRM.XNYS": 6.0}, "MOMENTUM-002": {"A.XNYS": 100.0}})   # GWRE gone, no own fill
    net = lane_net_terms(b, flows, coverage=COV, today=TODAY, qty_now=now)["1W"]
    assert net["TECHIVOL-005"]["invested"] is None
    assert net["TECHIVOL-005"]["partial"] == "cache cannot vouch for this window: GWRE held 14 at the base, 0 now, own fills 0 — moved without a fill"
    assert net["MOMENTUM-002"]["invested"] == -1000.0 and net["MOMENTUM-002"]["partial"] is None
    assert net["EXTERNAL"]["invested"] is None


def test_STAGING2_BOOT_PAIRS_are_vouch_NEUTRAL_named_and_MOMENTUM_stays_numeric():
    """staging2 /orders, 2026-09-11 12:08 ET: IB reconciliation at boot wrote, per held name, a SELL on
    a uuid coid at the entry price and a BUY on a `SYM.XNYS` coid a cent away — 7 pairs beside the 8
    real kumo- entries. Same lane, session, instrument, qty, opposite sides: net cash ≈ 0. Counted
    (the cent residual is inside the identity) and NAMED, never a refusal."""
    rows = {BASE: []}
    b = _bases(_Store(rows, captured={BASE}))
    names = [("CF.XNYS", 93, 133.22, 133.23), ("CVE.XNYS", 378, 32.925, 32.93), ("ECO.XNYS", 170, 74.944, 74.95),
             ("GRDN.XNYS", 284, 44.75, 44.76), ("PAA.XNAS", 490, 25.72, 25.73), ("SM.XNYS", 332, 37.796, 37.801),
             ("UGP.XNYS", 1653, 7.56, 7.566)]
    kf = []
    for iid, q, sell_px, buy_px in names:
        kf.append(_kf("MOMENTUM-002", "2026-09-11", iid, "SELL", q, sell_px, "inferred"))
        kf.append(_kf("MOMENTUM-002", "2026-09-11", iid, "BUY", q, buy_px, "inferred"))
    entries = 93 * 133.22 + 378 * 32.925 + 170 * 74.944 + 284 * 44.75 + 490 * 25.72 + 332 * 37.796 + 1653 * 7.56 + 190 * 65.88
    residual = sum(q * (bp - sp) for _, q, sp, bp in names)
    flows = _flows2({"MOMENTUM-002": {"2026-09-11": entries + residual}}, kernel_fills=kf,
                    touched={"MOMENTUM-002": {"2026-09-11": [n[0] for n in names] + ["CRAK.ARCX"]}})
    cov = _coverage(first_observed={"MOMENTUM-002": "2026-09-11"}, captured=COV["captured"])
    net = _terms(b, flows, coverage=cov, today=TODAY)["1W"]
    # THE PAIR'S RESIDUAL IS NOT CASH. The BUY leg a cent above the SELL leg is the kernel's
    # arithmetic, not money that left the account: invested is the real entries alone.
    assert residual != 0.0, "fixture: the pairs do not net to exactly zero, so the exclusion is testable"
    assert net["MOMENTUM-002"]["invested"] == pytest.approx(entries)
    assert net["MOMENTUM-002"]["partial"] == "first observed 2026-09-11; boot pairs: 7"


def test_a_MISMATCHED_pair_is_NOT_a_pair_and_refuses():
    """Qty differs (or one side missing): unpaired — refused, both fills named."""
    rows = {BASE: [_row("MOMENTUM-002", "CF.XNYS", BASE, 93, 130.0, 133.0)]}
    b = _bases(_Store(rows))
    flows = _flows2({"MOMENTUM-002": {"2026-09-11": 1.0}},
                    kernel_fills=[_kf("MOMENTUM-002", "2026-09-11", "CF.XNYS", "SELL", 93, 133.22, "inferred"),
                                  _kf("MOMENTUM-002", "2026-09-11", "CF.XNYS", "BUY", 90, 133.23, "inferred")],
                    touched={"MOMENTUM-002": {"2026-09-11": ["CF.XNYS"]}})
    net = _terms(b, flows, coverage=COV, today=TODAY)["1W"]
    assert net["MOMENTUM-002"]["invested"] is None
    assert net["MOMENTUM-002"]["partial"] == "cache cannot vouch for this window: CF.XNYS inferred 2026-09-11 (2) on MOMENTUM-002"


def test_a_dirty_fill_ON_the_base_day_still_refuses_and_the_day_BEFORE_does_not():
    rows = {BASE: [_row("MOMENTUM-002", "A.XNYS", BASE, 100, 15.0, 20.0)]}
    b = _bases(_Store(rows))
    on_base = _flows2({"MOMENTUM-002": {"2026-09-10": -1000.0}},
                      kernel_fills=[_kf("MOMENTUM-002", BASE, "A.XNYS", "SELL", 3, 20.0, "inferred")],
                      touched={"MOMENTUM-002": {BASE: ["A.XNYS"], "2026-09-10": ["A.XNYS"]}})
    assert _terms(b, on_base, coverage=COV, today=TODAY)["1W"]["MOMENTUM-002"]["invested"] is None
    before = _flows2({"MOMENTUM-002": {"2026-09-10": -1000.0}},
                     kernel_fills=[_kf("MOMENTUM-002", "2026-09-03", "A.XNYS", "SELL", 3, 20.0, "inferred")],
                     touched={"MOMENTUM-002": {"2026-09-03": ["A.XNYS"], "2026-09-10": ["A.XNYS"]}})
    assert _terms(b, before, coverage=COV, today=TODAY)["1W"]["MOMENTUM-002"]["invested"] == -1000.0


def test_an_OLD_flows_frame_without_kernel_fills_refuses_every_lane_never_vouches():
    """An engine at a4d37f7 publishes `internal` counts but no `kernel_fills`/`touched`: the api
    cannot scope, and must not read the absence as clean. The #1071 window-wide rule applies."""
    b = _bases(_Store(PAPER_ROWS))
    old = _flows({"MOMENTUM-002": {"2026-09-10": -1000.0}}, internal={"EXTERNAL": {"2026-09-08": {"reconciliation": 8}}})
    net = _terms(b, old, coverage=COV, today=TODAY)["1W"]
    assert net["MOMENTUM-002"]["invested"] is None and net["TECHIVOL-005"]["invested"] is None
    assert "reconciliation 2026-09-08 (8) on EXTERNAL" in net["TECHIVOL-005"]["partial"]


def test_partial_notes_COMPOSE_born_gap_and_internal_in_one_string():
    b = _bases(_Store(PAPER_ROWS))
    cov = _coverage(first_observed={**COV["first_observed"], "QC345-003": "2026-09-11"},
                    captured=COV["captured"] - {"2026-09-10"})
    flows = _flows({"QC345-003": {"2026-09-11": 2803.8}}, internal={"QC345-003": {"2026-09-11": {"transfer": 1}}})
    net = _terms(b, flows, coverage=cov, today=TODAY)
    assert net["1W"]["QC345-003"]["partial"] == (
        "first observed 2026-09-11; not captured: 2026-09-10; internal fills: transfer 2026-09-11 (1)")


def test_a_lane_FLAT_at_the_base_with_rows_the_day_BEFORE_has_mv_base_ZERO_not_yesterdays():
    """Review Q2(i): the base day is captured; the lane has rows on 09-03 and none on 09-04. It held
    nothing at the base — zero, and never the 09-03 value."""
    rows = {"2026-09-03": [_row("BCTROT-004", "B.XNYS", "2026-09-03", 10, 1.0, 100.0)],
            BASE: [_row("MOMENTUM-002", "A.XNYS", BASE, 100, 15.0, 20.0)]}
    b = _bases(_Store(rows))
    assert b.base_date["1W"] == BASE and "BCTROT-004" not in b.market_value["1W"]
    net = lane_net_terms(b, _flows({"BCTROT-004": {"2026-09-08": 50.0}}), coverage=COV, today=TODAY)
    assert net["1W"]["BCTROT-004"]["mv_base"] == 0.0 and net["1W"]["BCTROT-004"]["invested"] == 50.0




def _ts(day, hhmm):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return int(datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=ZoneInfo("America/New_York")).timestamp() * 1e9)


def test_a_REAL_same_day_round_trip_under_kernel_coids_is_NOT_a_boot_pair_it_REFUSES():
    """Review Q1: BCTROT/MOMENTUM enter at open+5m and give back at close−20m, same qty. After a
    cache recreate those fills can arrive under kernel coids — pairing them would take the trade's
    whole P&L out of the window under a benign "boot pairs: 1". A boot pair is written by
    reconciliation within seconds, a cent apart: both gates, or both legs stay UNPAIRED."""
    rows = {BASE: [_row("MOMENTUM-002", "A.XNYS", BASE, 100, 15.0, 20.0)]}
    b = _bases(_Store(rows))
    hours_apart = _flows2({"MOMENTUM-002": {"2026-09-10": -200.0}},
                          kernel_fills=[_kf("MOMENTUM-002", "2026-09-10", "CF.XNYS", "BUY", 93, 133.22, "inferred", _ts("2026-09-10", "09:35")),
                                        _kf("MOMENTUM-002", "2026-09-10", "CF.XNYS", "SELL", 93, 135.37, "inferred", _ts("2026-09-10", "15:40"))],
                          touched={"MOMENTUM-002": {"2026-09-10": ["CF.XNYS"]}})
    net = _terms(b, hours_apart, coverage=COV, today=TODAY)["1W"]["MOMENTUM-002"]
    assert net["invested"] is None and "CF.XNYS inferred 2026-09-10 (2) on MOMENTUM-002" in net["partial"]
    # seconds apart but 2% apart in price: also not a boot pair
    far_px = _flows2({"MOMENTUM-002": {"2026-09-10": -200.0}},
                     kernel_fills=[_kf("MOMENTUM-002", "2026-09-10", "CF.XNYS", "BUY", 93, 133.22, "inferred", _ts("2026-09-10", "12:08")),
                                   _kf("MOMENTUM-002", "2026-09-10", "CF.XNYS", "SELL", 93, 135.90, "inferred", _ts("2026-09-10", "12:08"))],
                     touched={"MOMENTUM-002": {"2026-09-10": ["CF.XNYS"]}})
    assert _terms(b, far_px, coverage=COV, today=TODAY)["1W"]["MOMENTUM-002"]["invested"] is None


def test_a_two_MIC_spelling_still_matches_the_instrument_a_lane_held():
    """Review (vi): the base row says LAND.XNYS, the kernel fill says LAND.XNAS (#1057's two-MIC
    names). The symbol is the identity for the vouch; a MIC drift must not read as clean."""
    rows = {BASE: [_row("MOMENTUM-002", "LAND.XNYS", BASE, 429, 9.4, 9.44)]}
    b = _bases(_Store(rows))
    # (a) an own kernel fill spelled on the other MIC still lands on the held name
    flows = _flows2({"MOMENTUM-002": {"2026-09-10": -200.0}},
                    kernel_fills=[_kf("MOMENTUM-002", "2026-09-08", "LAND.XNAS", "SELL", 429, 9.39, "inferred")],
                    touched={"MOMENTUM-002": {"2026-09-08": ["LAND.XNAS"]}},
                    net_qty={"MOMENTUM-002": {"2026-09-08": {"LAND.XNAS": -429.0}}})
    net = _terms(b, flows, coverage=COV, today=TODAY)["1W"]["MOMENTUM-002"]
    assert net["invested"] is None and "LAND.XNAS inferred 2026-09-08 (1) on MOMENTUM-002" in net["partial"]
    # (b) the invariant reads across MICs too: base LAND.XNYS 429, now LAND.XNAS 429, no fills → consistent
    clean = _flows2({"MOMENTUM-002": {}}, touched={}, net_qty={})
    ok = lane_net_terms(b, clean, coverage=COV, today=TODAY, qty_now=_now(**{"MOMENTUM-002": {"LAND.XNAS": 429.0}}))["1W"]["MOMENTUM-002"]
    assert ok["invested"] == 0.0 and ok["partial"] is None


def test_HOURS_apart_at_the_SAME_price_is_still_not_a_boot_pair_the_time_gate_stands_alone():
    """A flat round trip (entered and given back at the same price) fails only the TIME gate — a
    mutant dropping it survived the price-gap fixture above."""
    rows = {BASE: [_row("MOMENTUM-002", "A.XNYS", BASE, 100, 15.0, 20.0)]}
    b = _bases(_Store(rows))
    flat = _flows2({"MOMENTUM-002": {"2026-09-10": 0.0}},
                   kernel_fills=[_kf("MOMENTUM-002", "2026-09-10", "CF.XNYS", "BUY", 93, 133.22, "inferred", _ts("2026-09-10", "09:35")),
                                 _kf("MOMENTUM-002", "2026-09-10", "CF.XNYS", "SELL", 93, 133.22, "inferred", _ts("2026-09-10", "15:40"))],
                   touched={"MOMENTUM-002": {"2026-09-10": ["CF.XNYS"]}})
    assert _terms(b, flat, coverage=COV, today=TODAY)["1W"]["MOMENTUM-002"]["invested"] is None


def test_a_kernel_fill_WITHOUT_a_timestamp_cannot_pair_and_refuses():
    """An engine that carries kernel fills but no `ts_ns` cannot be told a boot pair from a trade."""
    rows = {BASE: [_row("MOMENTUM-002", "A.XNYS", BASE, 100, 15.0, 20.0)]}
    b = _bases(_Store(rows))
    a = _kf("MOMENTUM-002", "2026-09-10", "CF.XNYS", "BUY", 93, 133.22, "inferred"); a.pop("ts_ns")
    c = _kf("MOMENTUM-002", "2026-09-10", "CF.XNYS", "SELL", 93, 133.22, "inferred"); c.pop("ts_ns")
    flows = _flows2({"MOMENTUM-002": {"2026-09-10": 0.0}}, kernel_fills=[a, c], touched={"MOMENTUM-002": {"2026-09-10": ["CF.XNYS"]}})
    assert _terms(b, flows, coverage=COV, today=TODAY)["1W"]["MOMENTUM-002"]["invested"] is None


# -- #1072 b: the qty invariant, and EXTERNAL's boot mirrors ------------------------------------------

def test_STAGING2_EXTERNAL_BOOT_MIRRORS_do_NOT_refuse_the_lane_whose_position_they_mirror():
    """staging2 09-11 12:08:17 ET (fr2467iv's table): per name, EXTERNAL BUY tagged VENUE at the
    venue's avgCost + a same-second RECONCILIATION SELL at the lane's fill px — EXTERNAL net 0, the
    lane's position exactly its own kumo- fills. TECHIVOL's identity is closed: numeric."""
    rows = {BASE: []}
    b = _bases(_Store(rows, captured={BASE}))
    kf = [_kf("EXTERNAL", "2026-09-11", "CRWD.XNAS", "BUY", 24, 206.30, "venue", _ts("2026-09-11", "12:08")),
          _kf("EXTERNAL", "2026-09-11", "CRWD.XNAS", "SELL", 24, 206.25, "reconciliation", _ts("2026-09-11", "12:08"))]
    flows = _flows2({"TECHIVOL-005": {"2026-09-11": 24 * 206.25}, "EXTERNAL": {"2026-09-11": 24 * 0.05}},
                    kernel_fills=kf,
                    touched={"TECHIVOL-005": {"2026-09-11": ["CRWD.XNAS"]}, "EXTERNAL": {"2026-09-11": ["CRWD.XNAS"]}},
                    net_qty={"TECHIVOL-005": {"2026-09-11": {"CRWD.XNAS": 24.0}}, "EXTERNAL": {"2026-09-11": {"CRWD.XNAS": 0.0}}})
    cov = _coverage(first_observed={"TECHIVOL-005": "2026-09-11"}, captured=COV["captured"])
    net = lane_net_terms(b, flows, coverage=cov, today=TODAY, qty_now=_now(**{"TECHIVOL-005": {"CRWD.XNAS": 24.0}}))["1W"]
    assert net["TECHIVOL-005"]["invested"] == pytest.approx(24 * 206.25)
    assert net["TECHIVOL-005"]["partial"] == "first observed 2026-09-11"
    assert net["EXTERNAL"]["invested"] == pytest.approx(0.0), "the mirror pair's residual is not cash"
    assert net["EXTERNAL"]["partial"] == "boot pairs: 1"


def test_PAPER_GWRE_a_position_that_MOVED_WITHOUT_a_fill_of_its_own_is_refused_by_the_QTY_INVARIANT():
    """TECHIVOL held GWRE 14 at the base, holds 0 now, filled 0 of it itself: the shares left with no
    TECHIVOL fill (the kernel booked the sell under EXTERNAL). Refused, named by the invariant —
    whatever EXTERNAL's own fills look like."""
    rows = {BASE: [_row("TECHIVOL-005", "GWRE.XNYS", BASE, 14, 160.0, 162.45),
                   _row("TECHIVOL-005", "CRM.XNYS", BASE, 6, 250.0, 259.5),
                   _row("MOMENTUM-002", "A.XNYS", BASE, 100, 15.0, 20.0)]}
    b = _bases(_Store(rows))
    flows = _flows2({"TECHIVOL-005": {"2026-09-10": -1162.76}, "MOMENTUM-002": {"2026-09-10": -1000.0}, "EXTERNAL": {"2026-09-08": -2274.3}},
                    kernel_fills=[_kf("EXTERNAL", "2026-09-08", "GWRE.XNYS", "SELL", 14, 162.45, "reconciliation")],
                    touched={"EXTERNAL": {"2026-09-08": ["GWRE.XNYS"]}, "TECHIVOL-005": {"2026-09-10": ["CRM.XNYS"]}, "MOMENTUM-002": {"2026-09-10": ["A.XNYS"]}},
                    net_qty={"TECHIVOL-005": {"2026-09-10": {"CRM.XNYS": -4.0}}, "MOMENTUM-002": {"2026-09-10": {"A.XNYS": -50.0}}, "EXTERNAL": {"2026-09-08": {"GWRE.XNYS": -14.0}}})
    # CRM is CONSISTENT (6 → 2 by its own −4); only GWRE moved without a fill.
    now = _now(**{"TECHIVOL-005": {"CRM.XNYS": 2.0}, "MOMENTUM-002": {"A.XNYS": 50.0}})
    net = lane_net_terms(b, flows, coverage=COV, today=TODAY, qty_now=now)["1W"]
    assert net["TECHIVOL-005"]["invested"] is None
    assert net["TECHIVOL-005"]["partial"] == "cache cannot vouch for this window: GWRE held 14 at the base, 0 now, own fills 0 — moved without a fill"
    assert net["MOMENTUM-002"]["invested"] == -1000.0 and net["MOMENTUM-002"]["partial"] is None


def test_PAPER_LAND_an_UNPAIRED_inferred_fill_on_the_lane_ITSELF_still_refuses():
    rows = {BASE: [_row("MOMENTUM-002", "A.XNYS", BASE, 100, 15.0, 20.0)]}
    b = _bases(_Store(rows))
    flows = _flows2({"MOMENTUM-002": {"2026-09-08": 429 * 9.44}},
                    kernel_fills=[_kf("MOMENTUM-002", "2026-09-08", "LAND.XNAS", "BUY", 429, 9.44, "inferred")],
                    touched={"MOMENTUM-002": {"2026-09-08": ["LAND.XNAS"]}},
                    net_qty={"MOMENTUM-002": {"2026-09-08": {"LAND.XNAS": 429.0}}})
    now = _now(**{"MOMENTUM-002": {"A.XNYS": 100.0, "LAND.XNAS": 429.0}})
    net = lane_net_terms(b, flows, coverage=COV, today=TODAY, qty_now=now)["1W"]["MOMENTUM-002"]
    assert net["invested"] is None
    assert net["partial"] == "cache cannot vouch for this window: LAND.XNAS inferred 2026-09-08 (1) on MOMENTUM-002"


def test_a_CLEAN_lane_with_a_position_that_grew_by_exactly_its_own_fills_is_numeric():
    rows = {BASE: [_row("BCTROT-004", "B.XNYS", BASE, 10, 1.0, 100.0)]}
    b = _bases(_Store(rows))
    flows = _flows2({"BCTROT-004": {"2026-09-09": 500.0, "2026-09-11": -300.0}},
                    touched={"BCTROT-004": {"2026-09-09": ["B.XNYS"], "2026-09-11": ["B.XNYS"]}},
                    net_qty={"BCTROT-004": {"2026-09-09": {"B.XNYS": 5.0}, "2026-09-11": {"B.XNYS": -3.0}}})
    net = lane_net_terms(b, flows, coverage=COV, today=TODAY, qty_now=_now(**{"BCTROT-004": {"B.XNYS": 12.0}}))["1W"]["BCTROT-004"]
    assert net["invested"] == 200.0 and net["partial"] is None


def test_WITHOUT_a_positions_plane_the_invariant_cannot_be_checked_and_every_lane_is_refused():
    rows = {BASE: [_row("BCTROT-004", "B.XNYS", BASE, 10, 1.0, 100.0)]}
    b = _bases(_Store(rows))
    flows = _flows2({"BCTROT-004": {"2026-09-09": 500.0}}, touched={"BCTROT-004": {"2026-09-09": ["B.XNYS"]}},
                    net_qty={"BCTROT-004": {"2026-09-09": {"B.XNYS": 5.0}}})
    net = lane_net_terms(b, flows, coverage=COV, today=TODAY, qty_now=None)["1W"]["BCTROT-004"]
    assert net["invested"] is None and "positions" in net["partial"]


def test_an_OLD_frame_without_net_qty_cannot_be_checked_and_refuses():
    rows = {BASE: [_row("BCTROT-004", "B.XNYS", BASE, 10, 1.0, 100.0)]}
    b = _bases(_Store(rows))
    old = _flows2({"BCTROT-004": {"2026-09-09": 500.0}}, touched={"BCTROT-004": {"2026-09-09": ["B.XNYS"]}})
    old.pop("net_qty")
    net = lane_net_terms(b, old, coverage=COV, today=TODAY, qty_now=_now(**{"BCTROT-004": {"B.XNYS": 15.0}}))["1W"]["BCTROT-004"]
    assert net["invested"] is None


def test_the_FRAMES_qty_now_is_preferred_over_the_positions_plane_and_a_SHORT_lane_holds_the_invariant():
    """(1)+(2): the frame's own `qty_now` (same pass as `net_qty`) wins over the api plane; a lane
    that went 0 → −4 by its own −4 fill is consistent, signed."""
    rows = {BASE: []}
    b = _bases(_Store(rows, captured={BASE}))
    flows = _flows2({"BCTROT-004": {"2026-09-09": -400.0}}, touched={"BCTROT-004": {"2026-09-09": ["S.XNYS"]}},
                    net_qty={"BCTROT-004": {"2026-09-09": {"S.XNYS": -4.0}}})
    flows["qty_now"] = {"BCTROT-004": {"S.XNYS": -4.0}}
    stale_plane = _now(**{"BCTROT-004": {"S.XNYS": -3.0}})   # the api plane, one fill behind
    cov = _coverage(first_observed={"BCTROT-004": "2026-09-09"}, captured=COV["captured"])
    net = lane_net_terms(b, flows, coverage=cov, today=TODAY, qty_now=stale_plane)["1W"]["BCTROT-004"]
    assert net["invested"] == -400.0, "the frame's qty_now, not the stale plane, decides"


def test_a_SPLIT_doubles_the_qty_with_no_fill_and_is_refused_moved_without_a_fill():
    rows = {BASE: [_row("BCTROT-004", "B.XNYS", BASE, 10, 1.0, 100.0)]}
    b = _bases(_Store(rows))
    flows = _flows2({"BCTROT-004": {}}, touched={}, net_qty={})
    flows["qty_now"] = {"BCTROT-004": {"B.XNYS": 20.0}}
    net = lane_net_terms(b, flows, coverage=COV, today=TODAY, qty_now=None)["1W"]["BCTROT-004"]
    assert net["invested"] is None
    assert net["partial"] == "cache cannot vouch for this window: B held 10 at the base, 20 now, own fills 0 — moved without a fill"


def test_a_CLEAN_round_trip_flat_to_flat_on_own_fills_is_vouched():
    rows = {BASE: []}
    b = _bases(_Store(rows, captured={BASE}))
    flows = _flows2({"MOMENTUM-002": {"2026-09-10": 1000.0, "2026-09-11": -1100.0}},
                    touched={"MOMENTUM-002": {"2026-09-10": ["R.XNYS"], "2026-09-11": ["R.XNYS"]}},
                    net_qty={"MOMENTUM-002": {"2026-09-10": {"R.XNYS": 10.0}, "2026-09-11": {"R.XNYS": -10.0}}})
    flows["qty_now"] = {}
    cov = _coverage(first_observed={"MOMENTUM-002": "2026-09-10"}, captured=COV["captured"])
    net = lane_net_terms(b, flows, coverage=cov, today=TODAY, qty_now=None)["1W"]["MOMENTUM-002"]
    assert net["invested"] == -100.0 and net["partial"] == "first observed 2026-09-10"
