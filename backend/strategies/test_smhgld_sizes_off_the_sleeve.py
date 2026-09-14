"""SMHGLD sizes its targets off ITS SLEEVE, never off the account (cockpit#986).

Read from source on 2026-09-11 (#953 gateway at 1b3d883): `run()` took `equity` from
`read_equity(self._broker)` → `NautilusBroker.equity()` → the broker ACCOUNT message's `equity` —
Alpaca `portfolio_value`, or IB net liquidation in the account's BASE currency (SGD on staging2) — and
handed it to `order_plan(…, equity)`, so `target_qty = weight × ACCOUNT equity / price`. The sleeve
entered only afterwards, as `may_submit`'s refusal gate, which says no to a buy over the sleeve and
never resizes it. On paper (101k account) with a 20k sleeve both opening buys are `budget_refused`;
with the default 0 sleeve, UNFUNDED. Coded risk rows, no orders, every surface plausible — the sixth
silent-inert shape of the day, and the one every gate we built would have let through.

The research's `equity` is the sleeve's own capital, and the basis that agrees with the gate is
`min(actual, target)` (`Sleeve.deployable(0.0)`, api/budget.py; `may_submit` bounds entries by the
same number). What `actual` IS (read, not assumed — coverage review): `target` is the operator's
number from settings; `actual` is SET to net asset value by `budget_store.mark_to_market`, which has
NO production caller today, and otherwise moves only by transfers (`on_sell_fill`,
`distribute_unallocated`). So `actual` does not track the lane's P&L now; the day the mark is wired,
targets will drift with P&L and the rebalance cadence changes in a way the research did not model —
a note for that day, not a reason to size off the account. The sleeve table carries NO currency
field: IB's SGD net liquidation never reaches it, which is the only reason the SGD-read-as-USD error
disappears with this change — nothing is converted. Every test here drives the REAL gateway on the
executor tests' production-shaped doubles with the slot production passes; the plan is the real
upstream `order_plan`.
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from strategies.test_smhgld_delta_execution import _Broker, _Journal, _gateway

ACCOUNT = 100_000.0
SLEEVE = 20_000.0
PX = {"SMH": 565.49, "GLD": 398.50}     # last trades 2026-09-11 10:44Z
#: The sizing basis the plan must be handed: the sleeve minus one share of the priciest leg (#992).
BASIS = SLEEVE - max(PX.values())


def _upstream():
    try:
        from kumo_strategies.strategies.smhgld_sleeve.config import SmhGldSleeveConfig
        from kumo_strategies.strategies.smhgld_sleeve.engine import Decision, build_feature_panel, order_plan
    except ImportError:
        pytest.skip("installed kumo-strategies predates the SMHGLD sleeve (06f3055)")
    return SmhGldSleeveConfig, Decision, build_feature_panel, order_plan


def _panel(cfg, build_feature_panel):
    days = pd.bdate_range("2026-01-02", periods=320)
    rows = [{"ticker": s, "date": d, "open": p, "high": p * 1.01, "low": p * 0.99, "close": p, "volume": 1_000_000}
            for s, px in PX.items() for i, d in enumerate(days) for p in (px * (1 + 0.0004 * i),)]
    panel = build_feature_panel(pd.DataFrame(rows), cfg)
    return panel[panel["date"] == panel["date"].max()]


def _expected(order_plan, Decision, cfg, equity):
    w = {cfg.risk_asset: cfg.risk_weight, cfg.defensive_asset: 1 - cfg.risk_weight}
    return sorted((o.symbol, float(o.delta)) for o in order_plan(Decision(weights=w, regime="opening"), {}, PX, equity))


def _plan_recording(cfg, Decision, order_plan, seen: list):
    """The upstream `order_plan` on the opening decision, with the `equity` the gateway handed in
    RECORDED — so a test can assert which number reached the sizing, not only what came out."""
    w = {cfg.risk_asset: cfg.risk_weight, cfg.defensive_asset: 1 - cfg.risk_weight}

    def _plan(panel, session, *, held, prices, equity, lot=1):
        seen.append(float(equity))
        shares = {k: float(v) for k, v in dict(held or {}).items() if v}
        d = Decision(weights=w, regime="opening")
        return {"regime": d.regime, "weights": w}, tuple(order_plan(d, shares, dict(prices), float(equity), lot=float(lot)))
    return _plan


def _run(journal, broker, sleeve_actual, *, target=None, deployed=0.0, budget_raises=None, seen=None, state="TRADING"):
    """`target` defaults to a value the fix could NOT confuse with `actual` (agreement is not connection)."""
    from api.budget import Sleeve
    SmhGldSleeveConfig, Decision, build_feature_panel, order_plan = _upstream()
    cfg = SmhGldSleeveConfig()
    target = sleeve_actual * 3 if target is None else target
    gw = _gateway(state, journal, broker, _plan_recording(cfg, Decision, order_plan, seen if seen is not None else []),
                  sleeve=Sleeve("SMHGLD-007", target, sleeve_actual), deployed=deployed, budget_raises=budget_raises)
    return asyncio.run(gw.run(panel=_panel(cfg, build_feature_panel), session="2026-09-11", slot="close-20m"))


def _decision_detail(journal):
    rows = [r for r in journal.rows if r[0] == "decision"]
    assert rows, f"no decision row: {journal.rows[:3]}"
    kw = rows[-1][-1] if isinstance(rows[-1][-1], dict) else {}
    return kw.get("detail") or {}


def _submitted(broker):
    return sorted((o.symbol, float(o.qty)) for o in broker.submitted)


def test_FIXTURE_the_sleeve_and_the_account_size_DIFFERENTLY_so_this_file_can_tell_them_apart():
    """Two derivations that agree cannot detect which one is wired. 20k and 100k must not round to the
    same whole shares at these prices, or every assertion below is vacuous."""
    SmhGldSleeveConfig, Decision, _, order_plan = _upstream()
    cfg = SmhGldSleeveConfig()
    small, big = dict(_expected(order_plan, Decision, cfg, SLEEVE)), dict(_expected(order_plan, Decision, cfg, ACCOUNT))
    for leg in PX:
        assert small[leg] != big[leg], f"{leg}: 20k and 100k round to the same whole shares — the leg cannot discriminate"
        assert small[leg] >= 1, "fixture: the 20k opening is whole shares on both legs"
    assert sum(q * PX[leg] for leg, q in small.items()) <= SLEEVE, "fixture: the 20k plan FITS the sleeve, so a refusal below is sizing, not budget"


def test_the_opening_session_is_sized_off_the_SLEEVE_not_the_account():
    """A 100k account with a 20k sleeve opens the 20k plan — two whole-share buys — and NOT two
    `budget_refused` rows for a 100k plan the sleeve cannot fund."""
    SmhGldSleeveConfig, Decision, _, order_plan = _upstream()
    journal, broker, seen = _Journal(), _Broker(equity=ACCOUNT, held={}, prices=PX, buys_need_sells_first=False), []
    _run(journal, broker, SLEEVE, seen=seen)
    codes = [r for r in journal.rows if r[0] == "risk"]
    assert seen == [BASIS], f"the plan was handed equity={seen}, not the reserved sleeve basis"
    assert _submitted(broker) == _expected(order_plan, Decision, SmhGldSleeveConfig(), BASIS), (
        f"submitted {_submitted(broker)}; risk rows {codes[:3]}")


def test_the_ACCOUNT_number_appears_nowhere_in_the_sizing_path():
    """THE PROPERTY, not the arithmetic: change the account equity by four orders of magnitude and the
    orders do not move. A lane sized off the sleeve cannot see the account."""
    a, b = _Journal(), _Journal()
    small = _Broker(equity=ACCOUNT, held={}, prices=PX, buys_need_sells_first=False)
    huge = _Broker(equity=1e9, held={}, prices=PX, buys_need_sells_first=False)
    _run(a, small, SLEEVE)
    _run(b, huge, SLEEVE)
    assert _submitted(small) == _submitted(huge) and _submitted(small), "the account equity leaked into the sizing"


def test_an_UNREADABLE_sleeve_refuses_the_session_and_never_falls_back_to_the_account():
    """The old order of operations read the account first and the sleeve second, so a dead budget
    reader still produced a full-account plan. Now the sleeve is the input; unreadable → refused by
    code, zero orders, never a plan sized off something else."""
    journal, broker, seen = _Journal(), _Broker(equity=ACCOUNT, held={}, prices=PX, buys_need_sells_first=False), []
    _run(journal, broker, SLEEVE, budget_raises=RuntimeError("postgres away"), seen=seen)
    assert broker.submitted == []
    assert any(r[0] == "risk" and "budget_unreadable" in str(r[1:]) for r in journal.rows), journal.rows[:3]
    # THE DISCRIMINATOR: before the fix this test passed too — the rows and the empty submit list were
    # produced AFTER a 100k plan had been sized off the account. The plan must not have been asked.
    assert seen == [], f"a plan was sized (equity={seen}) before the sleeve was known to be unreadable"


def test_an_UNFUNDED_sleeve_refuses_the_opening_and_never_sizes_off_the_account():
    """`SMHGLD-007` defaults to 0 = UNFUNDED. With the account at 100k that must be a refusal by code,
    not a 100k plan that happens to be refused leg by leg."""
    journal, broker, seen = _Journal(), _Broker(equity=ACCOUNT, held={}, prices=PX, buys_need_sells_first=False), []
    _run(journal, broker, 0.0, seen=seen)
    assert broker.submitted == []
    assert any(r[0] == "risk" and "budget_unfunded" in str(r[1:]) for r in journal.rows), journal.rows[:3]
    assert seen == [], f"a plan was sized (equity={seen}) off something other than an unfunded sleeve"


def test_the_sizing_basis_is_the_GATE_S_derivation_min_of_actual_and_target():
    """Two derivations of one capital must match: `may_submit` bounds entries by
    `sleeve.deployable(deployed)` = min(actual, target) − deployed. Sizing off `actual` alone reproduces
    the defect in miniature whenever the sleeve is over target (is_reducing → every entry refused);
    sizing off `target` alone ignores what the sleeve physically has mid-transition. The basis is
    `min(actual, target)`: whichever binds."""
    a, b = [], []
    j1, br1 = _Journal(), _Broker(equity=ACCOUNT, held={}, prices=PX, buys_need_sells_first=False)
    _run(j1, br1, 20_000.0, target=30_000.0, seen=a)          # actual binds
    j2, br2 = _Journal(), _Broker(equity=ACCOUNT, held={}, prices=PX, buys_need_sells_first=False)
    _run(j2, br2, 30_000.0, target=20_000.0, seen=b)          # target binds (sleeve over target)
    assert a == [20_000.0 - max(PX.values())] and b == [20_000.0 - max(PX.values())], f"basis was actual={a} / target={b}; must be min(actual, target) − reserve"


def test_deployed_capital_does_not_change_the_sizing_basis_only_the_gate_s_room():
    """Targets are TOTAL desired holdings sized off the sleeve's capital; `deployed` is the gate's
    business (room = deployable − deployed). With 15k already deployed of a 20k sleeve the plan is
    still handed 20k — the gate then refuses what does not fit, by code."""
    seen = []
    j, br = _Journal(), _Broker(equity=ACCOUNT, held={}, prices=PX, buys_need_sells_first=False)
    _run(j, br, SLEEVE, deployed=15_000.0, seen=seen)
    assert seen == [BASIS]


def test_the_journal_detail_carries_the_SLEEVE_equity_not_the_account():
    """The decision row's `equity` is what an operator reads to check the sizing; the account number
    there would be the same lie one surface down."""
    j, br = _Journal(), _Broker(equity=ACCOUNT, held={}, prices=PX, buys_need_sells_first=False)
    _run(j, br, SLEEVE)
    detail = _decision_detail(j)
    assert float(detail.get("equity", -1)) == BASIS, f"detail equity {detail.get('equity')!r}"


def test_SHADOW_sizes_off_the_sleeve_too_and_an_unfunded_sleeve_refuses_before_shadowing():
    """SHADOW journals the intent it WOULD trade; an intent sized off the account is a wrong intent
    journaled with confidence. And an unfunded sleeve has no intent to shadow: refused by code first."""
    seen = []
    j, br = _Journal(), _Broker(equity=ACCOUNT, held={}, prices=PX, buys_need_sells_first=False)
    _run(j, br, SLEEVE, seen=seen, state="SHADOW")
    assert seen == [BASIS] and br.submitted == [] and float(_decision_detail(j).get("equity", -1)) == BASIS
    seen2 = []
    j2, br2 = _Journal(), _Broker(equity=ACCOUNT, held={}, prices=PX, buys_need_sells_first=False)
    _run(j2, br2, 0.0, seen=seen2, state="SHADOW")
    assert seen2 == [] and br2.submitted == [] and any(r[0] == "risk" and "budget_unfunded" in str(r[1:]) for r in j2.rows)


def test_a_FULL_sleeve_rotating_GLD_into_SMH_executes_BOTH_legs_the_sell_frees_the_room():
    """Scope review of #986: `deployed` was read once and reused for every leg, so a fully deployed
    sleeve rotating GLD→SMH got room ≈ 0, the SELL filled, the BUY was `budget_refused`, PARTIAL — a
    half-rebalanced book. Reachable only now that plans are sized to the sleeve. An accepted sell
    must free its notional for the buys that follow it (sells fund buys, #953's own rule)."""
    SmhGldSleeveConfig, Decision, _, order_plan = _upstream()
    cfg = SmhGldSleeveConfig()
    # a book deployed to within one GLD share of the sleeve, overweight GLD: the plan sells 6 GLD
    # and buys 5 SMH. NOT exactly at the sleeve: `order_plan` rounds toward the holding on BOTH legs,
    # so from a book exactly at the sleeve the post-trade book can exceed it by up to one share per
    # leg and the buy is refused for a true reason — the fixture must make the stale `deployed` the
    # ONLY thing that can refuse the buy.
    # MEASURED WHILE WRITING THIS (a separate finding, ticketed): with the sleeve EXACTLY equal to the
    # basis, round-toward-holding sells 5 GLD (40 → 35 = 13,948) and buys 5 SMH (6 → 11 = 6,220):
    # post-book 20,168 > 20,000, so the buy is refused by 168 for a TRUE reason — a rotation at
    # capacity can never complete. This test gives the sleeve 500 of slack so the stale `deployed` is
    # the only thing that can refuse the buy.
    held = {"SMH": 6, "GLD": 40}
    sleeve = SLEEVE + 500.0
    deployed = sum(q * PX[s] for s, q in held.items())
    assert sleeve - 1_500 < deployed < sleeve, f"fixture: the book is just under the sleeve ({deployed:,.0f} vs {sleeve:,.0f})"
    post = sum(q * PX[s] for s, q in {"SMH": 11, "GLD": 35}.items())
    assert deployed < post <= sleeve, f"fixture: the post-rotation book ({post:,.0f}) FITS the sleeve; a refusal can only be the stale room"
    journal, broker, seen = _Journal(), _Broker(equity=ACCOUNT, held=held, prices=PX, buys_need_sells_first=True), []
    from api.budget import Sleeve
    from strategies.test_smhgld_delta_execution import _gateway as _gw
    w = {cfg.risk_asset: cfg.risk_weight, cfg.defensive_asset: 1 - cfg.risk_weight}

    def plan(panel, session, *, held, prices, equity, lot=1):
        seen.append(equity)
        d = Decision(weights=w, regime="rebalance")
        return {"regime": "rebalance", "weights": w}, tuple(order_plan(d, {k: float(v) for k, v in held.items()}, dict(prices), float(equity), lot=float(lot)))
    gw = _gw("TRADING", journal, broker, plan, sleeve=Sleeve("SMHGLD-007", sleeve * 3, sleeve), deployed=deployed)
    asyncio.run(gw.run(panel=None, session="2026-09-11", slot="close-20m"))
    sides = sorted((o.side.name if hasattr(o.side, "name") else str(o.side), o.symbol) for o in broker.submitted)
    assert seen == [sleeve - max(PX.values())]
    assert sides == [("BUY", "SMH"), ("SELL", "GLD")], f"submitted {sides}; risk rows {[r for r in journal.rows if r[0] == 'risk'][:2]}"


# -- #992: one share of the priciest leg is RESERVED, so a rotation at capacity completes -------------------

def _rotation(sleeve_actual, *, deployed=None, held=None):
    """The #992 fixture: a book at the sleeve, overweight GLD; the plan sells GLD and buys SMH."""
    SmhGldSleeveConfig, Decision, _, order_plan = _upstream()
    cfg = SmhGldSleeveConfig()
    held = {"SMH": 6, "GLD": 40} if held is None else held
    deployed = sum(q * PX[s] for s, q in held.items()) if deployed is None else deployed
    journal, broker, seen = _Journal(), _Broker(equity=ACCOUNT, held=held, prices=PX, buys_need_sells_first=True), []
    from api.budget import Sleeve
    w = {cfg.risk_asset: cfg.risk_weight, cfg.defensive_asset: 1 - cfg.risk_weight}

    def plan(panel, session, *, held, prices, equity, lot=1):
        seen.append(equity)
        d = Decision(weights=w, regime="rebalance")
        return {"regime": "rebalance", "weights": w}, tuple(order_plan(d, {k: float(v) for k, v in held.items()}, dict(prices), float(equity), lot=float(lot)))
    gw = _gateway("TRADING", journal, broker, plan, sleeve=Sleeve("SMHGLD-007", sleeve_actual * 3, sleeve_actual), deployed=deployed)
    asyncio.run(gw.run(panel=None, session="2026-09-11", slot="close-20m"))
    sides = sorted((o.side.name if hasattr(o.side, "name") else str(o.side), o.symbol, float(o.qty)) for o in broker.submitted)
    return seen, sides, journal


def test_a_rotation_at_EXACT_capacity_COMPLETES_because_one_share_of_the_priciest_leg_is_reserved():
    """#992, ruled: rounding toward the holding makes a SELL leave more than target and a BUY take
    less, so the residuals have opposite signs and the sum overshoots the basis whenever the sell
    leg's residual exceeds the buy leg's (GLD +348, SMH −180 → +168 over 20,000; the buy correctly
    refused, forever). The residual cannot exceed one share of any single leg, so sizing against
    `basis − max(price over the legs)` bounds it by construction — deterministic, self-scaling (2.8%
    at 20k, 0.19% at 300k), read from the plan's own prices at sizing time, never a constant."""
    seen, sides, journal = _rotation(SLEEVE)
    assert seen == [SLEEVE - max(PX.values())], f"sizing basis {seen}; must be basis − max price"
    assert [s[:2] for s in sides] == [("BUY", "SMH"), ("SELL", "GLD")], f"submitted {sides}; risk {[r for r in journal.rows if r[0] == 'risk'][:2]}"


def test_the_reserve_does_not_make_the_gate_unreachable_a_genuinely_over_sleeve_buy_is_still_refused():
    """CONTROL: the gate must still bite. Report the book as already deployed to the FULL sleeve
    (a foreign/stale leg the sleeve counts against this lane): the sell frees its notional but the
    buy still exceeds the room, and is refused by code — the reserve narrows targets, it does not
    widen the gate."""
    seen, sides, journal = _rotation(SLEEVE, deployed=SLEEVE + max(PX.values()))
    assert seen == [SLEEVE - max(PX.values())]
    assert [s[:2] for s in sides] == [("SELL", "GLD")], f"submitted {sides}"
    assert any(r[0] == "risk" and "budget_refused" in str(r[1:]) for r in journal.rows)


def test_a_ZERO_target_with_capital_in_the_sleeve_is_REFUSED_by_name_never_planned_as_sell_everything():
    """ffv73l93, #993 review (BLOCK): `read_sleeve` refuses UNFUNDED only on actual <= 0, so target == 0
    with actual > 0 yields `deployable(0.0) == 0` → equity 0 → every target 0 shares → SELL EVERYTHING.
    A wind-down is an operator FLATTEN, not an inferred liquidation. Refused by its own code, with
    target and actual on the row, before any plan is asked."""
    seen = []
    j, br = _Journal(), _Broker(equity=ACCOUNT, held={"SMH": 11, "GLD": 34}, prices=PX, buys_need_sells_first=False)
    _run(j, br, 50_000.0, target=0.0, seen=seen)
    assert seen == [] and br.submitted == [], f"planned {seen}, submitted {[(o.symbol, o.qty) for o in br.submitted]}"
    rows = [r for r in j.rows if r[0] == "risk" and "sleeve_basis_zero" in str(r[1:])]
    assert len(rows) == 1, j.rows[:3]
    detail = (rows[0][-1] if isinstance(rows[0][-1], dict) else {}).get("detail") or {}
    assert detail.get("target") == 0.0 and detail.get("actual") == 50_000.0, detail


def test_a_sell_decrement_that_would_take_deployed_NEGATIVE_is_journaled_not_clamped_silently():
    """ffv73l93, #993 review (MEDIUM): `deployed` and the decrement must share a basis. Both are
    market-priced once #965's reader lands (claims × current price); until then a sold leg's gain can
    take the subtraction below zero. A clamp that says nothing hides that the book was smaller than
    the sleeve thought — journal it, then clamp."""
    seen, sides, journal = _rotation(SLEEVE, deployed=1_000.0)   # a book the sleeve thinks is nearly empty
    assert any(r[0] == "risk" and "deployed_below_zero" in str(r[1:]) for r in journal.rows), [r for r in journal.rows if r[0] == "risk"][:3]


def test_a_NON_FINITE_sleeve_is_refused_by_name_nan_is_not_a_basis():
    """Implementation review of #986: `nan <= 0.0` is False, `max(0.0, nan)` is 0.0, and the deleted
    `read_equity` carried the only `isfinite` check. A NaN `actual` (a bad settings read, a broken
    mark) must refuse by code, never reach `order_plan` — the `nan`-disarms-a-halt class."""
    import math
    seen = []
    j, br = _Journal(), _Broker(equity=ACCOUNT, held={"SMH": 11, "GLD": 34}, prices=PX, buys_need_sells_first=False)
    _run(j, br, math.nan, target=20_000.0, seen=seen)
    assert seen == [] and br.submitted == [], f"planned {seen}, submitted {[(o.symbol, o.qty) for o in br.submitted]}"
    assert any(r[0] == "risk" and ("sleeve_basis_zero" in str(r[1:]) or "budget_unfunded" in str(r[1:])) for r in j.rows), j.rows[:3]
