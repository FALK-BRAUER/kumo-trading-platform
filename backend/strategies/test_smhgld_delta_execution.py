"""SMHGLD delta execution (kumo-trading-platform issue 953): a TARGET and a DELTA, executed; never `enter`/`exit`.

The lane's contract is `order_plan()` (kumo-trading-strategies f36b674): a tuple of `Order(symbol, delta,
target_qty, held_qty, reason)` with `post_trade_qty = held + delta`, SORTED sells-first, deltas
already rounded to a whole lot, zero deltas ABSENT, and an empty tuple for a session in which the
regime is neither "opening" nor "rebalance" — a REAL decision, not a no-op.

Four things the gateway must NOT re-derive (coordinator, from l21's code): the ordering, the
rounding, the target, and the delta. The claim is not derived from the plan at all any more: submit
acceptance is local, while terminal order events sync the actual Nautilus cache quantity.

Every test drives `SmhgldSessionGateway.run(panel, session, slot=...)` — the exact call the adapter
makes (`smhgld_sleeve.py:214 self._runner.run(panel, str(session.date()), slot=slot)`). The
broker double refuses what `NautilusBroker.submit` refuses (its own tables, imported) and records
the ORDER of submissions, because "sells before buys" is a sequential requirement: a fully-invested
sleeve funds its buys with its sells.

Numbers no default produces: equity 98765.0, SMH 240.00, GLD 310.00, lot 1.

Seen red 2026-09-11 against b930bd2: `strategies.smhgld` does not exist.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass

import pytest

from api.budget import Sleeve

SID = "SMHGLD-007"
SESSION = "2026-09-11"
SLOT = "open+5m"
EQUITY = 98765.0
PX = {"SMH": 240.00, "GLD": 310.00}


# ----------------------------------------------------------------------------- doubles ---------
@dataclass(frozen=True)
class _Order:
    """The installed `Order` as the gateway reads it — fields and the `post_trade_qty` property.
    Pinned to the real dataclass by `test_the_order_double_matches_the_INSTALLED_contract`."""
    symbol: str
    delta: float
    target_qty: float
    held_qty: float
    reason: str

    @property
    def post_trade_qty(self) -> float:
        return self.held_qty + self.delta


def _plan(*orders, regime="rebalance"):
    """A `plan` callable in the builder's shape: `(panel, session, *, held, prices, equity, lot) ->
    (decision, orders)`. Orders are handed back EXACTLY as given — the ordering is the plan's."""
    def plan(panel, session, *, held, prices, equity, lot):
        return {"regime": regime, "weights": {"SMH": 0.32, "GLD": 0.68}}, tuple(orders)
    return plan


class _Journal:
    def __init__(self):
        self.rows: list[tuple[str, str, dict]] = []

    async def write(self, kind, summary, *, session, detail=None, symbol=None, correlation=None,
                    slot=None):
        self.rows.append((kind, summary, {"session": session, "slot": slot, "symbol": symbol,
                                          "detail": detail or {}}))
        return len(self.rows)

    async def decided_this_session(self, session, slot=None):
        return any(k == "decision" and r["session"] == session and r["slot"] == slot
                   for k, _s, r in self.rows)

    def coded(self, code, kind="risk"):
        return [r for k, _s, r in self.rows if k == kind and r["detail"].get("code") == code]

    def decision(self):
        return next(r for k, _s, r in self.rows if k == "decision")


class _Broker:
    """Refuses what the installed broker refuses; records submissions IN ORDER; can be told to refuse
    a symbol or to raise, and to REJECT any BUY that arrives before every SELL was accepted (the
    margin failure a fully-invested sleeve produces when buys go first)."""

    def __init__(self, *, equity=EQUITY, held=None, prices=None, fail=None, raise_for=(),
                 buys_need_sells_first=True, book_raises=None, price_missing=()):
        self._equity = equity
        self._held = dict({"SMH": 130, "GLD": 214} if held is None else held)
        self._prices = dict(PX if prices is None else prices)
        self._fail = fail or {}
        self._raise_for = set(raise_for)
        self._buys_need_sells_first = buys_need_sells_first
        self._book_raises = book_raises
        self._price_missing = set(price_missing)
        self.submitted = []
        self.accepted = []
        self._sells_pending = 0

    def equity(self):
        return self._equity

    def strategy_positions(self):
        if self._book_raises is not None:
            raise self._book_raises
        return dict(self._held)

    def last_price(self, symbol):
        return None if symbol in self._price_missing else self._prices.get(symbol)

    def submit(self, req):
        from kumo_strategies.runtime.executor.broker import OrderRequest, OrderResult
        from kumo_strategies.runtime.nautilus.broker import _ORDER_SIDES, _TIME_IN_FORCE

        assert isinstance(req, OrderRequest)
        self.submitted.append(req)
        if req.symbol in self._raise_for:
            raise RuntimeError("venue path broke")
        if req.time_in_force not in _TIME_IN_FORCE or req.side not in _ORDER_SIDES:
            return OrderResult(False, None, "refused by the installed tables", req)
        if int(req.qty) <= 0:
            return OrderResult(False, None, f"quantity {req.qty} is not positive", req)
        if req.symbol in self._fail:
            return OrderResult(False, None, self._fail[req.symbol], req)
        if req.side == "BUY" and self._buys_need_sells_first and any(
                r.side == "SELL" and r not in self.accepted for r in self.submitted[:-1]):
            return OrderResult(False, None, "insufficient buying power — a sell has not funded it", req)
        self.accepted.append(req)
        return OrderResult(True, req.client_order_id, "submitted via nautilus (submitted)", req)


class _Claims:
    def __init__(self):
        self.calls = []

    async def __call__(self, symbol, qty, px):
        self.calls.append((symbol, qty, px))


def _gateway(state, journal, broker, plan, *, sleeve=Sleeve(SID, 100_000.0, 100_000.0), deployed=0.0,
             claims="recorder", budget_raises=None, lot=1):
    from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State

    from strategies.smhgld import SmhgldSessionGateway

    async def _read():
        return Lifecycle(State(state), "test")

    async def _budget():
        if budget_raises is not None:
            raise budget_raises
        return sleeve, deployed

    gw = SmhgldSessionGateway(journal=journal, broker=broker, read_state=_read, plan=plan,
                              strategy_id=SID, read_budget=_budget,
                              write_claim=_Claims() if claims == "recorder" else claims, lot=lot)
    return gw


def _run(gw, session=SESSION, slot=SLOT):
    return asyncio.run(gw.run(panel=None, session=session, slot=slot))


def _sides(broker):
    return [(r.side, r.symbol, r.qty) for r in broker.submitted]


SELL_SMH = _Order("SMH", -12.0, 118.4, 130.0, "trim")     # target 118.4, held 130, delta rounded to -12
BUY_GLD = _Order("GLD", 9.0, 223.6, 214.0, "add")         # target 223.6, held 214, delta rounded to +9


# ------------------------------------------------------------------ fixture properties ---------
def test_the_fixture_plan_differs_from_its_targets_by_a_sub_lot_on_both_legs():
    """If target and post_trade rounded to the same number, a claim written from either would
    agree and the test could not tell them apart."""
    for o in (SELL_SMH, BUY_GLD):
        assert o.post_trade_qty != o.target_qty
        assert abs(o.post_trade_qty - o.target_qty) < 1.0
    assert SELL_SMH.post_trade_qty == 118.0 and BUY_GLD.post_trade_qty == 223.0


def test_the_broker_double_REJECTS_a_buy_that_arrives_before_its_sell_was_accepted():
    """The margin failure, modelled: buy-first on a fully-invested sleeve fails on cash."""
    from kumo_strategies.runtime.executor.broker import OrderRequest

    b = _Broker()
    buy = OrderRequest(symbol="GLD", side="BUY", qty=9, session=SESSION, strategy_id=SID)
    sell = OrderRequest(symbol="SMH", side="SELL", qty=12, session=SESSION, strategy_id=SID)
    assert b.submit(buy).ok is True, "a buy with no pending sell is fine (nothing to fund it from)"
    b2 = _Broker()
    b2._fail["SMH"] = "denied"
    assert b2.submit(sell).ok is False
    assert b2.submit(buy).ok is False and "buying power" in b2.submit(buy).detail


# --------------------------------------------------------------------- TRADING: deltas ---------
def test_TRADING_executes_the_plan_SEQUENTIALLY_sells_first_one_order_per_symbol():
    journal, broker = _Journal(), _Broker()
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD))

    result = _run(gw)

    assert _sides(broker) == [("SELL", "SMH", 12), ("BUY", "GLD", 9)]
    assert all(r.limit_px is None and r.time_in_force == "DAY" for r in broker.submitted)
    assert result.decided is True and result.submitted == 2
    assert result.exited == ("SMH",) and result.entered == ("GLD",)


def test_a_locally_ACCEPTED_order_writes_NO_claim_until_the_terminal_event():
    """The live SMHGLD failure. Nautilus accepted the SMH order locally, but restart replay showed
    only GLD filled; the submit-time claim left SMH booked even though the lane held 0."""
    journal, broker = _Journal(), _Broker()
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD))

    _run(gw)

    assert _sides(broker) == [("SELL", "SMH", 12), ("BUY", "GLD", 9)]
    assert gw.write_claim.calls == []


def test_sync_claim_writes_the_ACTUAL_book_quantity_not_the_planned_post_trade_qty():
    """The event side supplies the lane's cache quantity. It may be 0 on rejection, or differ from
    the planned `post_trade_qty` after a partial fill/restart replay; that actual number wins."""
    journal, broker = _Journal(), _Broker()
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD))

    asyncio.run(gw.sync_claim("SMH", 83, 568.53))
    asyncio.run(gw.sync_claim("GLD", 0, None))

    assert gw.write_claim.calls == [("SMH", 83, 568.53), ("GLD", 0, None)]


def test_a_REFUSED_SELL_withholds_every_later_BUY_and_reports_PARTIAL_with_both_named():
    """The sells fund the buys. A refused sell means the buy would ask for margin the sleeve does
    not have; sending it anyway is the intermittent failure, worse than the constant one."""
    journal, broker = _Journal(), _Broker(fail={"SMH": "denied: SMH not sellable today"})
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD))

    result = _run(gw)

    assert _sides(broker) == [("SELL", "SMH", 12)], "the BUY was never sent"
    assert journal.coded("submit_refused", kind="error")[0]["symbol"] == "SMH"
    rows = journal.coded("buy_withheld")
    assert len(rows) == 1 and rows[0]["symbol"] == "GLD" and "SMH" in rows[0]["detail"]["after"]
    assert result.submitted == 0 and result.blocked and "PARTIAL" in result.blocked
    assert journal.decision()["detail"]["gateway_refused"] == {"SMH": "submit_refused",
                                                                "GLD": "buy_withheld"}


def test_a_REFUSED_BUY_does_NOT_withhold_a_later_BUY_buys_compete_they_do_not_fund():
    """Asymmetric by decision (coordinator, #953): sells fund buys, buys compete for cash. GLD's buy
    refused, a smaller later buy (a third leg here) is still attempted; PARTIAL names both."""
    third = _Order("XYZ", 3.0, 51.6, 48.0, "add")
    journal, broker = _Journal(), _Broker(prices={**PX, "XYZ": 20.0}, held={"SMH": 130, "GLD": 214, "XYZ": 48},
                                          fail={"GLD": "denied: GLD buying power"})
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD, third))

    result = _run(gw)

    assert _sides(broker) == [("SELL", "SMH", 12), ("BUY", "GLD", 9), ("BUY", "XYZ", 3)]
    assert journal.decision()["detail"]["gateway_refused"] == {"GLD": "submit_refused"}
    assert result.submitted == 2 and "PARTIAL" in result.blocked and "GLD" in result.blocked
    assert journal.coded("buy_withheld") == []


def test_a_submit_that_RAISES_is_an_error_row_and_stops_the_session_the_same_way():
    journal, broker = _Journal(), _Broker(raise_for=("SMH",))
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD))

    result = _run(gw)

    assert _sides(broker) == [("SELL", "SMH", 12)]
    assert "RuntimeError: venue path broke" in journal.coded("submit_raised", kind="error")[0]["detail"]["error"]
    assert result.submitted == 0 and gw.write_claim.calls == []


def test_the_gateway_does_NOT_reorder_the_plan_and_REFUSES_one_that_is_not_sells_first():
    """Ordering is the plan's (sorted by delta upstream). A plan arriving buys-first is a contract
    violation — refused whole, coded, nothing sent — never silently re-sorted (a second derivation)."""
    journal, broker = _Journal(), _Broker()
    gw = _gateway("TRADING", journal, broker, _plan(BUY_GLD, SELL_SMH))

    result = _run(gw)

    assert broker.submitted == []
    rows = journal.coded("plan_unordered")
    assert len(rows) == 1 and rows[0]["detail"]["first_buy"] == "GLD"
    assert result.submitted == 0


def test_a_FRACTIONAL_delta_REFUSES_the_session_and_is_never_truncated():
    """`OrderRequest.qty` is an int and `NautilusBroker.submit` builds `Quantity.from_int`: there is
    no fractional path through the cockpit on either venue. A plan with lot != 1 is refused by
    name, not rounded a second time."""
    journal, broker = _Journal(), _Broker()
    gw = _gateway("TRADING", journal, broker, _plan(_Order("SMH", -12.5, 117.5, 130.0, "trim")))

    result = _run(gw)

    assert broker.submitted == []
    rows = journal.coded("fractional_delta")
    assert len(rows) == 1 and rows[0]["symbol"] == "SMH" and rows[0]["detail"]["delta"] == -12.5
    assert result.submitted == 0


def test_an_EMPTY_plan_is_a_REAL_decision_journaled_as_zero_orders_not_a_failure():
    """A quiet week. `decided` True, `submitted` 0, no `blocked`, and the decision row says so."""
    journal, broker = _Journal(), _Broker()
    gw = _gateway("TRADING", journal, broker, _plan(regime="on_target"))

    result = _run(gw)

    assert broker.submitted == [] and result.decided is True and result.submitted == 0
    assert result.blocked is None
    d = journal.decision()
    assert d["detail"]["orders"] == [] and d["detail"]["regime"] == "on_target"
    assert "0 orders" in d["summary"] if "summary" in d else True
    assert any("0 orders" in s for k, s, _r in journal.rows if k == "decision")


# ------------------------------------------------------------ inputs: refuse, never guess ------
@pytest.mark.parametrize("equity", [None, float("nan"), 0.0, -1.0], ids=["None", "nan", "zero", "neg"])
def test_the_ACCOUNT_equity_is_NOT_an_input_a_broker_that_cannot_answer_it_still_plans_off_the_sleeve(equity):
    """#986: the account's net liquidation used to be the sizing basis and a bad one refused the
    session (`equity_unknown`). It is not an input any more — the sleeve is — so a broker whose
    `equity()` is None, nan, zero or negative changes NOTHING: the plan is asked with the sleeve's
    capital and the same orders go. (The refusal-before-plan property moved to the sleeve:
    `test_an_UNREADABLE_or_UNFUNDED_sleeve_refuses_the_WHOLE_session…` below.)"""
    calls = []

    def plan(panel, session, **kw):
        calls.append(kw); return {"regime": "rebalance", "weights": {}}, ()

    journal, broker = _Journal(), _Broker(equity=equity)
    gw = _gateway("TRADING", journal, broker, plan)

    result = _run(gw)

    assert [c["equity"] for c in calls] == [100_000.0 - max(PX.values())], "the plan must be sized off the sleeve (less one share of the priciest leg, #992), whatever the account says"
    assert journal.coded("equity_unknown") == [] and result.decided is True


def test_a_MISSING_price_for_a_weight_symbol_refuses_the_whole_session_by_name():
    """A target computed from a missing price is not a size; the other leg must not trade alone."""
    journal, broker = _Journal(), _Broker(price_missing=("GLD",))
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD))

    result = _run(gw)

    rows = journal.coded("price_missing")
    assert len(rows) == 1 and rows[0]["symbol"] == "GLD"
    assert broker.submitted == [] and result.decided is False


def test_an_UNREADABLE_book_refuses_the_whole_session():
    journal, broker = _Journal(), _Broker(book_raises=RuntimeError("cache offline"))
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD))

    result = _run(gw)

    rows = journal.coded("book_unreadable", kind="error")
    assert len(rows) == 1 and "RuntimeError: cache offline" in rows[0]["detail"]["error"]
    assert broker.submitted == [] and result.decided is False


@pytest.mark.parametrize("sleeve", [None, Sleeve(SID, 100_000.0, 0.0)], ids=["absent", "actual-0"])
def test_an_ABSENT_or_UNFUNDED_sleeve_refuses_the_WHOLE_session_sells_included(sleeve):
    """RULED (coordinator, 2026-09-11 11:10Z, #986): the sleeve is the sizing basis, so with no sleeve
    there is no basis — for sells too. A sell sized from a bogus equity is a bogus trim, not a safe
    exit; letting it through would ship the comforting half of a defect. The operator's exit is
    FLATTEN, which reads the live book and never this sleeve. Before #986 this test pinned "buys
    refused, sells still go" — a half-executed plan from an unknown basis."""
    journal, broker = _Journal(), _Broker()
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD), sleeve=sleeve)

    result = _run(gw)

    assert broker.submitted == [], "nothing may be sent from a sleeve that cannot size anything"
    rows = journal.coded("budget_unfunded")
    assert len(rows) == 1 and rows[0]["detail"]["value"] == (None if sleeve is None else 0.0)
    assert result.decided is False and "budget_unfunded" in (result.blocked or "") and "flatten" in (result.blocked or "")


def test_no_claim_writer_wired_refuses_the_WHOLE_session_not_just_the_claim():
    """A sent order whose terminal event cannot sync claims leaves the ledger stale. The writer is a
    precondition of sending, not a follow-up."""
    journal, broker = _Journal(), _Broker()
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD), claims=None)

    result = _run(gw)

    assert broker.submitted == []
    assert len(journal.coded("claims_unwired")) == 1 and result.decided is False


# ------------------------------------------------ no flat state: absence is an INCIDENT --------
def test_a_weight_symbol_HELD_AT_ZERO_with_no_order_for_it_is_an_INCIDENT_row_not_a_resting_state():
    """There is no flat state on this lane. GLD target > 0, held 0, and the plan did not touch it
    (an on_target regime cannot happen with a leg missing — so this is exactly the state that must
    never resolve quietly)."""
    journal, broker = _Journal(), _Broker(held={"SMH": 130, "GLD": 0})
    gw = _gateway("TRADING", journal, broker, _plan(regime="on_target"))

    _run(gw)

    rows = journal.coded("no_position", kind="incident")
    assert len(rows) == 1 and rows[0]["symbol"] == "GLD"
    assert rows[0]["detail"]["held"] == 0 and rows[0]["detail"]["weight"] == 0.68


def test_a_weight_symbol_held_at_zero_that_the_plan_OPENS_is_NOT_an_incident():
    """An entry is a delta from zero; the opening session's zero is the plan doing its job."""
    journal, broker = _Journal(), _Broker(held={"SMH": 130, "GLD": 0})
    open_gld = _Order("GLD", 216.0, 216.6, 0.0, "opening")
    gw = _gateway("TRADING", journal, broker, _plan(open_gld, regime="opening"))

    _run(gw)

    assert journal.coded("no_position", kind="incident") == []
    assert _sides(broker) == [("BUY", "GLD", 216)]


# ------------------------------------------------------------ lifecycle: same rules as #941 ----
def test_SHADOW_journals_the_plan_and_sends_nothing():
    journal, broker = _Journal(), _Broker()
    gw = _gateway("SHADOW", journal, broker, _plan(SELL_SMH, BUY_GLD))

    result = _run(gw)

    assert broker.submitted == [] and gw.write_claim.calls == []
    d = journal.decision()["detail"]
    assert d["shadow"] is True and [o["symbol"] for o in d["orders"]] == ["SMH", "GLD"]
    assert result.decided is True and result.submitted == 0


def test_LIQUIDATING_sends_nothing_and_names_liquidate_lane():
    journal, broker = _Journal(), _Broker()
    gw = _gateway("LIQUIDATING", journal, broker, _plan(SELL_SMH, BUY_GLD))

    result = _run(gw)

    assert broker.submitted == []
    assert journal.coded("liquidating")[0]["detail"]["mechanism"] == "liquidate_lane"
    assert result.submitted == 0


def test_a_slot_already_decided_is_not_decided_twice():
    journal, broker = _Journal(), _Broker()
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD))
    _run(gw)
    n = len(broker.submitted)

    result = _run(gw)

    assert len(broker.submitted) == n and result.blocked and "already decided" in result.blocked


# ------------------------------------------------------------------ protection + seams ---------
def test_the_lane_DECLARES_no_protection_IN_THE_REGISTRY_and_nowhere_else():
    """Decided, written down, pinned (coordinator): a fixed-weight sleeve rebalancing ~93×/yr has no
    stop in its measured design, and a stop sized to the holding would be tripped by its own
    trims (the PEAK cancel-and-replace class, 2026-08-12). `"none"` is a declared mode, so absence
    is a statement, never an oversight.

    ONE DERIVATION (#1029). This test used to pin `smhgld.PROTECTION_MODE == "none"` — a module
    constant nothing read, while the plane resolved the lane to TRAIL from a settings schema that had
    no key for it. The stance is `StrategyEntry.protection`, which `lane_modes` reads; a builder
    module carrying its own copy is a second source that merely agrees, so none may exist."""
    import ast
    import pathlib

    from api.protection import PROTECTION_MODES
    from api.strategy_registry import by_id

    entry = by_id("SMHGLD-007")
    assert entry.protection == "none" and "none" in PROTECTION_MODES
    assert "own trims" in entry.protection_reason or "own rebalancing" in entry.protection_reason

    for path in sorted(pathlib.Path(__file__).parent.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        names = {t.id for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Assign)
                 for t in n.targets if isinstance(t, ast.Name)}
        assert not names & {"PROTECTION_MODE", "PROTECTION_REASON"}, (
            f"{path.name} assigns a protection stance of its own — the registry is the one place")


def test_the_gateway_run_signature_binds_the_adapters_call():
    """`smhgld_sleeve.py:214`: `self._runner.run(panel, str(session.date()), slot=slot)`."""
    from strategies.smhgld import SmhgldSessionGateway

    sig = inspect.signature(SmhgldSessionGateway.run)
    sig.bind(object(), "panel", "2026-09-11", slot="open+5m")
    sig.bind(object(), "panel", "2026-09-11")


def test_the_order_double_matches_the_INSTALLED_contract():
    """Field names, order and the `post_trade_qty` property, pinned to the real dataclass; and the
    real `order_plan` returns sells-first with zero deltas absent — the two facts this gateway
    trusts without re-deriving."""
    from dataclasses import fields

    from kumo_strategies.strategies.smhgld_sleeve.engine import Order, order_plan

    assert [f.name for f in fields(Order)] == [f.name for f in fields(_Order)]
    real = Order("SMH", -12.0, 118.4, 130.0, "trim")
    assert real.post_trade_qty == _Order("SMH", -12.0, 118.4, 130.0, "trim").post_trade_qty == 118.0
    sig = inspect.signature(order_plan)
    assert list(sig.parameters) == ["decision", "shares", "prices", "equity", "lot"]
    assert sig.parameters["lot"].default == 1.0 and sig.parameters["lot"].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------- the coordinator's three conditions (#953) ------
def _pos(iid, lane, qty, price):
    return {"instrument_id": iid, "strategy_id": lane, "quantity": abs(qty),
            "side": "LONG" if qty > 0 else "SHORT", "market_value": abs(qty) * price}


def test_a_lane_that_DECLARES_none_is_a_named_opt_out_and_the_naked_detector_stays_awake_for_everyone_else():
    """Conditions 1 and 2. `mode_of` answers `LaneProtection("none")` for SMHGLD and the documented
    default for a lane with NO row. The SAME planner, driven once with both: SMHGLD's leg is REFUSED
    by name (`opted_out`, exposure kept in the report — a positive, machine-readable state), and the
    undeclared lane's naked leg still gets a stop intent. A detector that skipped lanes it could not
    find protection for would produce no `opted_out` row at all."""
    from api.protection import DEFAULT_LANE_PROTECTION, LaneProtection, plan_protection

    rows = [_pos("SMH.XNAS", SID, 130, 240.0), _pos("GLD.ARCX", SID, 214, 310.0),
            _pos("XYZ.XNAS", "MOMENTUM-002", 50, 100.0)]
    modes = {SID: LaneProtection(mode="none", source="settings")}
    plan = plan_protection(
        positions=rows, orders=[],
        atr_by_symbol={"SMH.XNAS": 5.0, "GLD.ARCX": 3.0, "XYZ.XNAS": 2.0},
        price_by_symbol={"SMH.XNAS": 240.0, "GLD.ARCX": 310.0, "XYZ.XNAS": 100.0},
        lane_of=lambda coid: "", mode_of=lambda lane: modes.get(lane, DEFAULT_LANE_PROTECTION))

    opted = sorted((r.instrument_id, r.reason, r.uncovered_notional) for r in plan.refusals)
    assert opted == [("GLD.ARCX", "opted_out", 214 * 310.0), ("SMH.XNAS", "opted_out", 130 * 240.0)]
    assert [i.instrument_id for i in plan.intents] == ["XYZ.XNAS"], "the undeclared lane is still protected"
    # no row and declared none are DIFFERENT states: the undeclared lane never reads as opted out
    assert not any(r.instrument_id == "XYZ.XNAS" for r in plan.refusals)


def test_the_operator_FLATTEN_handles_an_SMHGLD_shaped_holding_it_is_the_only_emergency_control_here():
    """Condition 3. With no stops, flatten is the lane's only emergency control, so 'it works for
    the other lanes' is now the assumption the whole lane rests on. Driven: LONG 130 SMH → SELL 130."""
    from decimal import Decimal

    from api import flatten

    d = flatten.decide(position_side="LONG", live_qty=Decimal("130"), expected_side="LONG",
                       expected_qty=Decimal("130"), resting_reducing_qty=Decimal("0"), market_open=True)
    assert d.action == "EXECUTE" and (d.order.side, d.order.quantity) == ("SELL", Decimal("130"))
    d2 = flatten.decide(position_side="LONG", live_qty=Decimal("214"), expected_side="LONG",
                        expected_qty=Decimal("214"), resting_reducing_qty=Decimal("0"), market_open=False)
    assert d2.action in ("QUEUE", "EXECUTE"), "outside RTH the flatten is queued for the open, never dropped"


# ---------------------------------------------- the boundary (Operator: "double fix then") ---------
def test_the_executor_is_LANE_AGNOSTIC_by_source_and_the_gateway_holds_the_policy():
    """`delta_execution.py` must never learn a lane: no SMHGLD name, no symbol, no protection mode,
    no regime word. ks#184 (momentum `rebalance_band`) is the second consumer; a lane name inside
    the executor is where the second consumer becomes a rewrite."""
    import inspect

    from strategies import delta_execution, smhgld

    src = inspect.getsource(delta_execution)
    body = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    body = body.split('"""', 2)[-1]  # drop the module docstring, which names the consumers on purpose
    for word in ("SMHGLD", "SmhGld", '"SMH"', '"GLD"', "PROTECTION_MODE", "no_flat_state", "regime",
                 "weights"):
        assert word not in body, f"the executor learned a lane: {word!r}"
    assert inspect.signature(delta_execution.execute_plan).parameters.keys() >= {
        "orders", "broker", "write_claim", "row", "sleeve", "deployed", "budget_code", "prices",
        "session", "slot", "strategy_id"}
    assert smhgld.SmhgldSessionGateway.__init__.__code__.co_varnames[:1] == ("self",)
    assert "symbols" in inspect.signature(smhgld.SmhgldSessionGateway).parameters


def test_the_symbols_are_INJECTED_prices_are_read_for_them_not_for_a_literal():
    """A gateway built for a different universe prices that universe. Bitten by: `{"SMH", "GLD"}`
    hard-coded in the price read."""
    asked = []

    def rp(broker, symbols):
        asked.append(sorted(symbols)); return {s: 10.0 for s in symbols}

    journal, broker = _Journal(), _Broker(held={"AAA": 5, "BBB": 7})
    from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State
    from strategies.smhgld import SmhgldSessionGateway

    async def _read():
        return Lifecycle(State("TRADING"), "test")

    async def _budget():
        return Sleeve(SID, 1000.0, 1000.0), 0.0

    gw = SmhgldSessionGateway(journal=journal, broker=broker, read_state=_read,
                              plan=lambda panel, session, **kw: ({"regime": "on_target",
                                                                  "weights": {"AAA": 0.5, "BBB": 0.5}}, ()),
                              strategy_id=SID, read_budget=_budget, write_claim=_Claims(),
                              symbols=("AAA", "BBB"), read_prices=rp)
    _run(gw)
    assert asked == [["AAA", "BBB"]]


def test_the_decision_row_carries_lot_and_equity_beside_the_weights():
    """A target is `weight * equity / price`; a row with the weights but not the equity cannot be
    re-derived by its reader (l21)."""
    journal, broker = _Journal(), _Broker()
    gw = _gateway("TRADING", journal, broker, _plan(SELL_SMH, BUY_GLD))

    _run(gw)

    d = journal.decision()["detail"]
    # `equity` is the SLEEVE's deployable capital (#986), never the account's 98,765.
    assert d["equity"] == 100_000.0 - max(PX.values()) and d["lot"] == 1 and d["weights"] == {"SMH": 0.32, "GLD": 0.68}


def test_the_INSTALLED_kumo_strategies_carries_the_seam_this_gateway_needs():
    """ks#177 (SMHGLD by target and delta) and ks#185 (`EXECUTES_DELTAS`) merged 2026-09-11 into
    kumo-trading-strategies 06f3055. Until then the two tests below were `xfail(not installed, strict=True)`
    — CONDITIONAL markers that go silently INERT the day the pin moves, which is the opposite of the
    ruled "something must FAIL if the conversion is forgotten". Now: the installed package MUST carry
    the seam. On a pin that predates it this fails by name, which is the signal that the pyproject pin
    was not moved with this PR — not a reason to put the markers back."""
    import importlib

    try:
        caps = importlib.import_module("kumo_strategies.runtime.nautilus.capabilities")
        sleeve = importlib.import_module("kumo_strategies.strategies.smhgld_sleeve")
    except ImportError as exc:
        pytest.fail(f"installed kumo_strategies predates ks#177/ks#185 ({exc}) — move backend/pyproject.toml's "
                    f"pin to >= 06f3055 (the gate's reference; versions.lock is the deploy's and is separate)")
    assert hasattr(caps, "EXECUTES_DELTAS"), "installed kumo_strategies predates ks#185 — move the pin to >= 06f3055"
    assert hasattr(sleeve, "order_plan") and hasattr(sleeve, "Order"), "installed kumo_strategies predates ks#177"
    assert not any(m.name == "xfail" for f in (test_the_order_double_matches_the_INSTALLED_contract,
                                               test_the_gateway_declares_EXECUTES_DELTAS_by_IDENTITY_not_by_name)
                   for m in getattr(f, "pytestmark", [])), "the conditional xfail markers are back"


def test_the_gateway_declares_EXECUTES_DELTAS_by_IDENTITY_not_by_name():
    """l21's gate: `offers()` does `any(d is capability for d in declared)`. A string spelling the
    name, `True`, or a look-alike sentinel are all refused."""
    from kumo_strategies.runtime.nautilus.capabilities import EXECUTES_DELTAS

    from strategies.smhgld import SmhgldSessionGateway

    assert any(d is EXECUTES_DELTAS for d in SmhgldSessionGateway.EXECUTES)
    # the real token's own sentence, read off the object — no default: the first version's
    # `getattr(..., "means", <the expected text>)` would have passed against a token with no `means`
    assert "sells before buys" in EXECUTES_DELTAS.means


def test_the_capability_tuple_is_EXACTLY_the_one_upstream_token_never_a_look_alike():
    """The placeholders `EXECUTES_DELTAS = True` and `executes_deltas = True` were both refused by
    design; nothing of that shape survives here. And the tuple is not padded: exactly one token, the
    upstream object. (Before 06f3055 this test had an `if not installed: assert EXECUTES == ()` branch
    — an empty tuple as a valid shape on an old pin; gone with the try/except in smhgld.py.)"""
    from kumo_strategies.runtime.nautilus.capabilities import EXECUTES_DELTAS

    from strategies import smhgld

    assert not hasattr(smhgld, "EXECUTES_DELTAS") and not hasattr(smhgld.SmhgldSessionGateway, "executes_deltas")
    assert smhgld.SmhgldSessionGateway.EXECUTES == (EXECUTES_DELTAS,)
    assert smhgld.SmhgldSessionGateway.EXECUTES[0] is EXECUTES_DELTAS
    # l21 (review of b36ef94): `EXECUTES = EXECUTES_DELTAS` (no comma) makes offers() iterate the token,
    # hit TypeError, and return False — refused as "declares nothing" rather than "declared it wrong".
    # A tuple, pinned, turns a confusing refusal into an obvious one; and the INSTANCE path is what
    # kumo_strategies' adapter checks (`require(session_runner, …)` at construction, the object passed
    # straight through), so it is pinned beside the class attribute.
    from kumo_strategies.runtime.nautilus.capabilities import offers

    assert isinstance(smhgld.SmhgldSessionGateway.EXECUTES, tuple)
    assert offers(smhgld.SmhgldSessionGateway, EXECUTES_DELTAS)
    assert offers(_gateway("TRADING", _Journal(), _Broker(held={}), _plan(regime="hold")), EXECUTES_DELTAS)


# ------------------------------------------ the opening session: ALL BUYS, no sell to run -------
def test_an_ALL_BUY_opening_plan_executes_both_legs_from_zero_and_files_no_incident():
    """THE LARGEST TRADE THIS LANE WILL EVER MAKE (coordinator, #965): it enters from zero on both legs
    in one session while every later session trades ~0.75% of the sleeve. The sells-before-buys path
    has no sells to run that day; a loop that assumed at least one sell would fail on the one session
    that matters most. Both buys go, no submit-time claim is written, no `no_position` incident (the
    plan opens both), the notionals split ~32/68 of the SLEEVE."""
    open_smh = _Order("SMH", 131.0, 131.69, 0.0, "opening")    # 0.32 * 98765 / 240 = 131.69 → 131
    open_gld = _Order("GLD", 216.0, 216.62, 0.0, "opening")    # 0.68 * 98765 / 310 = 216.62 → 216
    journal, broker = _Journal(), _Broker(held={})
    gw = _gateway("TRADING", journal, broker, _plan(open_smh, open_gld, regime="opening"))

    result = _run(gw)

    assert _sides(broker) == [("BUY", "SMH", 131), ("BUY", "GLD", 216)]
    assert gw.write_claim.calls == []
    assert journal.coded("no_position", kind="incident") == []
    assert journal.coded("buy_withheld") == [] and result.blocked is None
    assert result.submitted == 2 and result.entered == ("SMH", "GLD") and result.exited == ()
    notional = {s["symbol"]: s["qty"] * PX[s["symbol"]] for s in journal.decision()["detail"]["sent"]}
    assert abs(notional["SMH"] / EQUITY - 0.32) < 0.01 and abs(notional["GLD"] / EQUITY - 0.68) < 0.01
