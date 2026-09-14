"""Per-lane protection modes in the planner (#872).

THE MEASUREMENT THIS EXISTS FOR. The account-wide peak-relative trail closed QC345-003's BE (+31.2%)
and INTC (+23.4%) on 2026-09-11, both near the day's low, on a lane whose exit rule is its own monthly
rebalance. Over six independent 60-session periods the trail wins 2 of 6 and costs 82.5pp summed,
against an entry-relative floor's 10.7pp (kumo-strategies#139). The operator approved exempting QC345-003.

WHAT A FLOOR IS, precisely, because two of the four sibling defects live in this paragraph:

  * The trigger is `lane avg_px_open - k x ATR`, computed ONCE at placement and NEVER modified. Only
    QUANTITY is ever modified afterwards (the existing `Oversize` path). ATR growth after placement
    changes nothing.
  * The entry is THE LANE'S OWN, from the NETTING position `{instrument}-{strategy_id}`. The broker's
    account-level average mixes lanes; using it would put one lane's floor at another lane's entry.
  * A scale-in re-weights `avg_px_open`, so the ADDED shares get a SECOND floor at the new entry — a
    ladder, by design. Flat->reopen resets the entry and starts a fresh floor.
  * A SELL stop at or above the last price is REJECTED pre-submit by the venue. The trail can never hit
    this (it carries no absolute trigger); a floor can. So it is a named refusal, never a submit —
    nothing burns a client order id per tick.
"""

from __future__ import annotations

import pytest

from api.protection import (
    DEFAULT_LANE_PROTECTION,
    LaneProtection,
    PROTECTION_COID_PREFIX,
    REFUSAL_REASONS,
    Refusal,
    StopIntent,
    lane_modes,
    plan_protection,
)

_QC345 = "QC345-003"
_TECHIVOL = "TECHIVOL-005"
_MOMENTUM = "MOMENTUM-002"

#: Tonight's live QC345 book (#872 scope v2, measured 2026-09-11): entry, qty, ATR14, last.
_TONIGHT = {
    "AMAT.XNAS": {"entry": 475.73, "qty": 6, "atr": 16.03, "last": 458.48, "floor": 451.69},
    "MU.XNAS": {"entry": 945.33, "qty": 3, "atr": 44.51, "last": 980.79, "floor": 878.56},
    "STX.XNAS": {"entry": 824.48, "qty": 3, "atr": 43.83, "last": 860.96, "floor": 758.74},
}


def _row(iid: str, lane: str, qty: float, price: float) -> dict:
    return {"instrument_id": iid, "strategy_id": lane, "quantity": abs(qty),
            "side": "LONG" if qty > 0 else "SHORT", "market_value": abs(qty) * price}


def _resting(iid: str, coid: str, otype: str, qty: float, *, side: str = "sell",
             trigger: float | None = None) -> dict:
    return {"id": f"venue-{coid}", "client_order_id": coid, "instrument_id": iid,
            "symbol": iid.rsplit(".", 1)[0], "side": side, "status": "NEW", "type": otype,
            "order_type": otype, "qty": qty, "quantity": qty, "filled_qty": 0.0,
            "stop_price": trigger}


def _modes(**by_lane: LaneProtection):
    """`mode_of` from an explicit per-lane mapping. Anything unnamed is the documented default."""
    table = {lane.replace("_", "-"): policy for lane, policy in by_lane.items()}
    return lambda lane: table.get(lane, DEFAULT_LANE_PROTECTION)


def _plan(rows, orders=(), *, mode_of=None, entry_by_lane=None, atr=None, price=None,
          lane_of=None, multiple=1.5):
    iids = {r["instrument_id"] for r in rows} | {str(o["instrument_id"]) for o in orders}
    return plan_protection(
        positions=list(rows),
        orders=list(orders),
        atr_by_symbol=atr if atr is not None else {i: 5.0 for i in iids},
        price_by_symbol=price if price is not None else {i: 100.0 for i in iids},
        multiple=multiple,
        lane_of=lane_of if lane_of is not None else (lambda coid: _OWNER.get(coid, "")),
        mode_of=mode_of,
        entry_by_lane=entry_by_lane,
    )


#: Which lane placed which resting order, as the engine's cache answers it. Module-level so a test can
#: extend it for its own coids rather than each test rebuilding an attribution rule.
_OWNER: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _clear_owners():
    _OWNER.clear()
    yield
    _OWNER.clear()


# ---------------------------------------------------------------------------------------------
# (a) FIXTURE PROPERTY. Before any assertion about entry_floor, the fixture must be able to express
#     the behaviour it is replacing — otherwise a passing entry_floor test says nothing about which
#     branch ran.
# ---------------------------------------------------------------------------------------------

def test_the_fixture_produces_a_TRAILING_intent_under_mode_trail():
    plan = _plan([_row("AMAT.XNAS", _QC345, 6, 475.0)],
                 mode_of=_modes(QC345_003=LaneProtection("trail", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 475.73})

    assert [i.kind for i in plan.intents] == ["trail"]
    assert plan.intents[0].trail_bps > 0
    assert plan.intents[0].trigger_px is None
    assert plan.wrong_mode == []


# ---------------------------------------------------------------------------------------------
# (b) THE LANE'S OWN ENTRY. Two lanes, two entries, ONE instrument -> two different triggers.
# ---------------------------------------------------------------------------------------------

def test_two_lanes_on_one_instrument_get_floors_at_their_OWN_entries():
    """The account-level average is one number for both lanes. If it were the source, these two
    triggers would be EQUAL — which is the mutant this test exists to kill, and it is exactly the
    shape of [[agreement-is-not-connection]]: one entry for two lanes looks like agreement."""
    rows = [_row("AMAT.XNAS", _QC345, 6, 460.0), _row("AMAT.XNAS", _TECHIVOL, 4, 460.0)]
    entries = {("AMAT.XNAS", _QC345): 475.73, ("AMAT.XNAS", _TECHIVOL): 400.00}

    # FIXTURE PROPERTY: the two entries differ, so a shared-entry mutant CAN be detected here.
    assert entries[("AMAT.XNAS", _QC345)] != entries[("AMAT.XNAS", _TECHIVOL)]

    plan = _plan(rows,
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings"),
                                TECHIVOL_005=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane=entries,
                 atr={"AMAT.XNAS": 16.03}, price={"AMAT.XNAS": 458.48})

    by_lane = {i.strategy_id: i for i in plan.intents}
    assert set(by_lane) == {_QC345, _TECHIVOL}
    assert by_lane[_QC345].kind == "entry_floor"
    assert by_lane[_QC345].trigger_px == pytest.approx(475.73 - 1.5 * 16.03, abs=1e-9)
    assert by_lane[_TECHIVOL].trigger_px == pytest.approx(400.00 - 1.5 * 16.03, abs=1e-9)
    assert by_lane[_QC345].quantity == 6 and by_lane[_TECHIVOL].quantity == 4


def test_a_lane_multiple_overrides_the_domain_width_for_that_lane_only():
    """The lane-level `atrMultiple` is a knob, and it is tested with 2.25 — a value the domain default
    of 1.5 could not produce."""
    rows = [_row("AMAT.XNAS", _QC345, 6, 460.0), _row("AMAT.XNAS", _TECHIVOL, 4, 460.0)]
    plan = _plan(rows,
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", 2.25, source="settings"),
                                TECHIVOL_005=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 475.73, ("AMAT.XNAS", _TECHIVOL): 475.73},
                 atr={"AMAT.XNAS": 16.03}, price={"AMAT.XNAS": 458.48}, multiple=1.5)

    by_lane = {i.strategy_id: i.trigger_px for i in plan.intents}
    assert by_lane[_QC345] == pytest.approx(475.73 - 2.25 * 16.03, abs=1e-9)
    assert by_lane[_TECHIVOL] == pytest.approx(475.73 - 1.5 * 16.03, abs=1e-9)


def test_entry_floor_with_no_entry_for_the_lane_is_a_NAMED_refusal():
    """Three states. "we do not know this lane's entry" must not silently become the account's, nor a
    stop at some default distance from the last price — it is its own answer."""
    plan = _plan([_row("AMAT.XNAS", _QC345, 6, 460.0)],
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={})

    assert plan.intents == []
    assert [(r.instrument_id, r.reason) for r in plan.refusals] == [("AMAT.XNAS", "no_lane_entry")]
    assert plan.uncovered_notional > 0


# ---------------------------------------------------------------------------------------------
# (c) THE TRIGGER IS SET ONCE. A later pass may resize the order and may never re-price it.
# ---------------------------------------------------------------------------------------------

def test_a_resting_floor_is_never_re_priced_when_the_entry_moves():
    """The floor rests at 451.69. The lane scales in, the entry moves to 470, ATR grows — and the
    resting order is untouched: it is neither an intent, nor an oversize, nor a wrong-mode cancel."""
    _OWNER["PROT-SELL-AMAT-XNAS-0"] = _QC345
    resting = _resting("AMAT.XNAS", "PROT-SELL-AMAT-XNAS-0", "STOP_MARKET", 6, trigger=451.69)

    plan = _plan([_row("AMAT.XNAS", _QC345, 6, 470.0)], [resting],
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 470.00},
                 atr={"AMAT.XNAS": 25.0}, price={"AMAT.XNAS": 465.0})

    assert plan.intents == []
    assert plan.oversize == []
    assert plan.wrong_mode == []
    assert "AMAT.XNAS" in plan.covered_instrument_ids


def test_a_partial_exit_shrinks_the_floor_by_QUANTITY_and_nothing_else():
    """Held drops 6 -> 3 with a 6-share floor resting. The correction is a quantity, and `Oversize`
    carries no price at all — there is no shape here in which a trigger could be re-sent."""
    _OWNER["PROT-SELL-AMAT-XNAS-0"] = _QC345
    resting = _resting("AMAT.XNAS", "PROT-SELL-AMAT-XNAS-0", "STOP_MARKET", 6, trigger=451.69)

    plan = _plan([_row("AMAT.XNAS", _QC345, 3, 460.0)], [resting],
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 475.73},
                 atr={"AMAT.XNAS": 16.03}, price={"AMAT.XNAS": 458.48})

    assert [(o.instrument_id, o.target_quantity) for o in plan.oversize] == [("AMAT.XNAS", 3.0)]
    assert not hasattr(plan.oversize[0], "trigger_px")
    assert plan.intents == []


# ---------------------------------------------------------------------------------------------
# (d) OPTED OUT is neither covered nor naked.
# ---------------------------------------------------------------------------------------------

def test_mode_none_is_a_named_refusal_that_keeps_its_exposure_in_the_report():
    """A case classified as an exception drops out of the accounting and its exposure lands in
    whichever bucket is left over — "protected". `Refusal` exists so the term stays KNOWN, not zero."""
    plan = _plan([_row("AMAT.XNAS", _QC345, 6, 460.0)],
                 mode_of=_modes(QC345_003=LaneProtection("none", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 475.73})

    assert plan.intents == []
    assert [(r.reason, r.uncovered_notional) for r in plan.refusals] == [("opted_out", 6 * 460.0)]
    assert "AMAT.XNAS" not in plan.covered_instrument_ids
    assert plan.uncovered_notional == 6 * 460.0


def test_opting_out_does_not_cancel_a_stop_that_is_already_resting():
    """DELIBERATELY NOT DONE. `none` says "place nothing"; reading it as "tear down what is there"
    would strip live protection off a book the moment a settings key changed, and nothing re-arms it.
    Stated here so the absence is a decision rather than an oversight."""
    _OWNER["PROT-SELL-AMAT-XNAS-0"] = _QC345
    resting = _resting("AMAT.XNAS", "PROT-SELL-AMAT-XNAS-0", "TRAILING_STOP_MARKET", 6)

    plan = _plan([_row("AMAT.XNAS", _QC345, 6, 460.0)], [resting],
                 mode_of=_modes(QC345_003=LaneProtection("none", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 475.73})

    assert plan.wrong_mode == []


# ---------------------------------------------------------------------------------------------
# (e) THE CLASS GUARD. No per-lane policy anywhere -> the plan is what it was before #872.
# ---------------------------------------------------------------------------------------------

def _mixed_book():
    rows = [_row("AMAT.XNAS", _QC345, 6, 460.0),
            _row("MU.XNAS", _MOMENTUM, 3, 980.0),
            _row("STX.XNAS", _TECHIVOL, 3, 860.0),
            _row("NOW.XNYS", "MANUAL-001", 2, 900.0),
            _row("AEM.XNYS", "BCTROT-004", 54, 180.0)]
    _OWNER["PROT-SELL-MU-XNAS-0"] = _MOMENTUM
    orders = [_resting("MU.XNAS", "PROT-SELL-MU-XNAS-0", "TRAILING_STOP_MARKET", 3)]
    return rows, orders


def test_with_no_lane_policy_at_all_the_plan_is_byte_identical_to_todays():
    """Three readings of "nothing configured" — no callable, an empty settings dict, and a settings
    dict carrying every lane's schema DEFAULT — must produce one plan. Two derivations of one fact
    disagree; here all three are the pre-#872 behaviour and the equality is the detector."""
    rows, orders = _mixed_book()
    entries = {("AMAT.XNAS", _QC345): 475.73, ("MU.XNAS", _MOMENTUM): 945.33}

    absent = _plan(rows, orders, mode_of=None, entry_by_lane=entries)
    empty_settings = _plan(rows, orders, mode_of=lane_modes({}), entry_by_lane=entries)
    all_defaults = _plan(
        rows, orders,
        mode_of=lane_modes({f"{lane}_protection": {"mode": "trail"}
                            for lane in (_QC345, _MOMENTUM, _TECHIVOL, "MANUAL-001", "BCTROT-004")}),
        entry_by_lane=entries,
    )

    # FIXTURE PROPERTY: the book is not empty, so "identical" is a statement about real content.
    assert absent.intents and absent.covered_instrument_ids
    assert absent == empty_settings == all_defaults
    assert all(i.kind == "trail" for i in absent.intents)
    assert absent.wrong_mode == []


# ---------------------------------------------------------------------------------------------
# (f) AN UNATTRIBUTABLE LEG ON AN OVERRIDDEN INSTRUMENT IS A REFUSAL, never the default mode.
# ---------------------------------------------------------------------------------------------

def test_a_blank_lane_leg_on_an_instrument_an_override_touches_is_refused():
    """`attribute_rows_to_lanes` keeps `strategy_id: ""` when the cache does not sum to the broker.
    On an instrument some lane has opted out of, "we could not attribute these shares" must not
    resolve to "so trail them" — that is absence read as permission."""
    plan = _plan([_row("AMAT.XNAS", "", 6, 460.0)],
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 475.73})

    assert plan.intents == []
    assert [(r.instrument_id, r.reason) for r in plan.refusals] == [("AMAT.XNAS", "lane_unattributable")]


def test_a_blank_lane_leg_on_an_instrument_NO_override_touches_keeps_todays_behaviour():
    """The other side of the same three states, and the reason the refusal is scoped to instruments an
    override actually touches: a book with one exempt lane must not stop protecting everything else."""
    plan = _plan([_row("AEM.XNYS", "", 54, 180.0)],
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 475.73})

    assert [i.kind for i in plan.intents] == ["trail"]
    assert plan.refusals == []


# ---------------------------------------------------------------------------------------------
# (g) THE SIBLING THAT WOULD HAVE KILLED THE FIRST DEPLOY.
# ---------------------------------------------------------------------------------------------

def test_a_floor_at_or_above_the_last_price_is_refused_and_never_submitted():
    """Alpaca rejects a SELL stop whose trigger is at or above the last price (exec_client.py:826,
    :1173) — pre-submit, so every tick would burn a client order id for an order that cannot exist.
    The trail can never hit this; it carries no absolute trigger. AMAT tonight is one bad session away."""
    plan = _plan([_row("AMAT.XNAS", _QC345, 6, 445.0)],
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 475.73},
                 atr={"AMAT.XNAS": 16.03}, price={"AMAT.XNAS": 445.00})

    # FIXTURE PROPERTY: the floor really is above the last price here, so the branch is reachable.
    assert 475.73 - 1.5 * 16.03 > 445.00

    assert plan.intents == []
    assert [(r.instrument_id, r.reason) for r in plan.refusals] == [("AMAT.XNAS", "floor_below_market")]


def test_a_floor_below_the_last_price_is_placed():
    """The control for the refusal above — without it the previous test passes on a planner that
    refuses every floor."""
    plan = _plan([_row("AMAT.XNAS", _QC345, 6, 458.0)],
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 475.73},
                 atr={"AMAT.XNAS": 16.03}, price={"AMAT.XNAS": 458.48})

    assert [i.kind for i in plan.intents] == ["entry_floor"]


# ---------------------------------------------------------------------------------------------
# (h) THE TRANSITION. A correctly-sized trail COUNTS as coverage, so the floor is never planned while
#     it rests, and the shares it reserves make place-then-cancel impossible.
# ---------------------------------------------------------------------------------------------

def test_a_resting_trail_on_an_entry_floor_lane_is_a_cancel_and_no_floor_is_planned_that_pass():
    _OWNER["PROT-SELL-AMAT-XNAS-0"] = _QC345
    trail = _resting("AMAT.XNAS", "PROT-SELL-AMAT-XNAS-0", "TRAILING_STOP_MARKET", 6)
    rows = [_row("AMAT.XNAS", _QC345, 6, 458.0)]
    modes = _modes(QC345_003=LaneProtection("entry_floor", source="settings"))
    entries = {("AMAT.XNAS", _QC345): 475.73}

    first = _plan(rows, [trail], mode_of=modes, entry_by_lane=entries,
                  atr={"AMAT.XNAS": 16.03}, price={"AMAT.XNAS": 458.48})

    assert [w.order["client_order_id"] for w in first.wrong_mode] == ["PROT-SELL-AMAT-XNAS-0"]
    assert first.wrong_mode[0].strategy_id == _QC345
    # NO floor on this pass: the trail still reserves the shares, so a submit would be rejected on
    # `available: 0`. The naked window is one 60s tick per leg, one leg at a time, and it is the
    # cheapest of the three options — place-then-cancel is impossible and cancel-then-place-in-one-pass
    # would need a venue round trip inside the tick.
    assert first.intents == []

    # SECOND PASS, the trail gone. This is what makes the cancel a transition rather than a strip.
    second = _plan(rows, [], mode_of=modes, entry_by_lane=entries,
                   atr={"AMAT.XNAS": 16.03}, price={"AMAT.XNAS": 458.48})

    assert [(i.kind, round(i.trigger_px, 2)) for i in second.intents] == [("entry_floor", 451.69)]


def test_a_resting_trail_on_a_TRAIL_lane_is_left_alone():
    """The bucket is scoped to lanes that have actually opted out. A symmetric rule would cancel every
    fixed stop on every trail lane — bracket legs, PEAK stops, an operator's own — none of which this
    reconciler placed and none of which it puts back."""
    _OWNER["PROT-SELL-MU-XNAS-0"] = _MOMENTUM
    trail = _resting("MU.XNAS", "PROT-SELL-MU-XNAS-0", "TRAILING_STOP_MARKET", 3)

    plan = _plan([_row("MU.XNAS", _MOMENTUM, 3, 980.0)], [trail],
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={})

    assert plan.wrong_mode == []


def test_a_stop_this_reconciler_did_not_place_is_never_cancelled_as_wrong_mode():
    """PEAK arms trailing stops too, and they carry no `PROT-` id. Cancelling one would take a manager's
    exit off a live position on a settings change."""
    _OWNER["MGR-PEAK-AMAT-1"] = _QC345
    peak = _resting("AMAT.XNAS", "MGR-PEAK-AMAT-1", "TRAILING_STOP_MARKET", 6)

    plan = _plan([_row("AMAT.XNAS", _QC345, 6, 458.0)], [peak],
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 475.73})

    assert plan.wrong_mode == []
    assert peak["client_order_id"].startswith(PROTECTION_COID_PREFIX) is False


def test_a_trail_whose_lane_cannot_be_named_is_never_cancelled():
    """`lane_of` returning "" means the cache cannot say whose stop this is. Cancelling on a guess is
    how another lane's protection disappears."""
    trail = _resting("AMAT.XNAS", "PROT-SELL-AMAT-XNAS-0", "TRAILING_STOP_MARKET", 6)  # not in _OWNER

    plan = _plan([_row("AMAT.XNAS", _QC345, 6, 458.0)], [trail],
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 475.73})

    assert plan.wrong_mode == []


# ---------------------------------------------------------------------------------------------
# (i) THE LADDER, by design.
# ---------------------------------------------------------------------------------------------

def test_a_scale_in_adds_a_SECOND_floor_at_the_new_entry():
    """The resting floor keeps its trigger (it is never re-priced) and the added shares get their own
    at the re-weighted entry. Two rungs, deliberately: re-pricing the first would move a live stop, and
    leaving the new shares bare would report a covered position while part of it had nothing under it."""
    _OWNER["PROT-SELL-AMAT-XNAS-0"] = _QC345
    resting = _resting("AMAT.XNAS", "PROT-SELL-AMAT-XNAS-0", "STOP_MARKET", 6, trigger=451.69)

    plan = _plan([_row("AMAT.XNAS", _QC345, 9, 465.0)], [resting],
                 mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                 entry_by_lane={("AMAT.XNAS", _QC345): 470.00},
                 atr={"AMAT.XNAS": 16.03}, price={"AMAT.XNAS": 465.0})

    assert [(i.kind, i.quantity) for i in plan.intents] == [("entry_floor", 3.0)]
    assert plan.intents[0].trigger_px == pytest.approx(470.00 - 1.5 * 16.03, abs=1e-9)
    assert plan.oversize == [] and plan.wrong_mode == []


# ---------------------------------------------------------------------------------------------
# VERIFY BY DISAGREEMENT — the two modes over tonight's real book.
# ---------------------------------------------------------------------------------------------

def test_the_two_modes_disagree_on_every_name_in_tonights_QC345_book():
    """Two derivations of "where does QC345's stop sit" over the SAME measured book. Identical on any
    name would mean a dead mechanism — the mode selected and then discarded, which is the shape four
    swept `PortfolioConfig` flags had in kumo-strategies."""
    rows = [_row(iid, _QC345, d["qty"], d["last"]) for iid, d in _TONIGHT.items()]
    atr = {iid: d["atr"] for iid, d in _TONIGHT.items()}
    price = {iid: d["last"] for iid, d in _TONIGHT.items()}
    entries = {(iid, _QC345): d["entry"] for iid, d in _TONIGHT.items()}

    trailed = _plan(rows, mode_of=_modes(QC345_003=LaneProtection("trail", source="settings")),
                    entry_by_lane=entries, atr=atr, price=price)
    floored = _plan(rows, mode_of=_modes(QC345_003=LaneProtection("entry_floor", source="settings")),
                    entry_by_lane=entries, atr=atr, price=price)

    # FIXTURE PROPERTY: both arms priced every name, so "they differ" is not "one of them is empty".
    assert len(trailed.intents) == len(floored.intents) == 3
    assert trailed.refusals == [] and floored.refusals == []

    trail_level = {i.instrument_id: _TONIGHT[i.instrument_id]["last"] * (1 - i.trail_bps / 10_000)
                   for i in trailed.intents}
    floor_level = {i.instrument_id: i.trigger_px for i in floored.intents}

    for iid, expected in ((k, v["floor"]) for k, v in _TONIGHT.items()):
        assert floor_level[iid] == pytest.approx(expected, abs=0.01), iid
        assert abs(trail_level[iid] - floor_level[iid]) > 0.01, f"{iid}: the two modes agree"


# ---------------------------------------------------------------------------------------------
# THE CLOSED SETS. A reason nothing declares is a reason `/health.failed_requests` renders as noise.
# ---------------------------------------------------------------------------------------------

def test_every_new_refusal_reason_is_declared():
    assert {"opted_out", "lane_unattributable", "floor_below_market", "no_lane_entry"} <= REFUSAL_REASONS


def test_an_undeclared_refusal_reason_cannot_be_constructed():
    with pytest.raises(ValueError):
        Refusal("AMAT.XNAS", "because_i_felt_like_it", 100.0)


def test_a_malformed_intent_cannot_be_constructed():
    """The two kinds carry DIFFERENT fields, and a consumer dispatches on `kind`. An entry_floor intent
    with no trigger, or a trail intent carrying one, is a shape whose consumer would guess."""
    with pytest.raises(ValueError):
        StopIntent("AMAT.XNAS", "SELL", 6, 500, 3.5, None, _QC345, kind="entry_floor", trigger_px=None)
    with pytest.raises(ValueError):
        StopIntent("AMAT.XNAS", "SELL", 6, 500, 3.5, None, _QC345, kind="trail", trigger_px=451.69)
