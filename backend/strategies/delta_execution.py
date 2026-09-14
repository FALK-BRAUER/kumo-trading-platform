"""Delta execution — the lane-AGNOSTIC half of executing a target-and-delta plan (kumo-cockpit#953).

Takes `tuple[Order, ...]` (symbol, delta, target_qty, held_qty, reason, post_trade_qty), a submit
seam, a claim writer, a sleeve, prices and a journal-row writer — and knows NOTHING about which
lane produced the plan. SMHGLD is its first consumer (`strategies/smhgld.py`); kumo-strategies#184
(`rebalance_band` on the momentum family: "trade every position to a target, emit the difference,
sells before buys") is the second, and the coordinator's constraint is that the second must be a
WIRING change, not a rewrite — two derivations of one mechanism drift. Nothing here may reach for a
lane's config, symbols, protection mode or regime vocabulary; `test_delta_execution_is_lane_agnostic`
walks this file's source for the words.

WHAT IT GUARANTEES, per plan:
  * at most one order per symbol, SEQUENTIAL, in the plan's order — never re-sorted (a plan that is
    not sells-first is refused whole by the caller before it gets here);
  * a refused or raising SELL WITHHOLDS every later BUY (`buy_withheld`): sells FUND buys;
  * a refused BUY does NOT withhold later buys: buys COMPETE for cash, a smaller one may still fund;
  * buys are budget-gated (`api.budget_gate.may_submit`), sells never are — reducing exposure needs
    no budget; an absent, zero or unreadable sleeve refuses BUYS by name (never budget_gate's
    unknown=allowed);
  * on acceptance the claim is set ABSOLUTELY to `post_trade_qty` — never `target_qty`, never a
    delta (the two differ by up to a lot on every resize, and nothing would ever disagree with a
    claim written from the unrounded target);
  * every refusal is a CODED row: submit_refused, submit_raised, buy_withheld, budget_refused,
    budget_unfunded, budget_unreadable.
"""
from __future__ import annotations

import math
from typing import Awaitable, Callable

RowWriter = Callable[..., Awaitable[object]]


async def read_sleeve(read_budget, row: RowWriter, *, session: str):
    """(sleeve, deployable-so-far, blocking_code). None reader / raising reader → UNREADABLE; absent
    or zero sleeve → UNFUNDED. `execute_plan` gates only BUYS on the code; the calling gateway
    refuses the WHOLE session on a code before any plan is sized (#986, ruled 2026-09-11): the
    sleeve is the sizing basis, a sell from no basis is not a safe exit, and the operator's exit is
    `flatten`, which never reads the sleeve."""
    if read_budget is None:
        await row("risk", "budget_unreadable", "no budget reader attached — the session is refused (the "
                  "sleeve is the sizing basis; an operator exit is `flatten`)", session=session, input="sleeve", error="read_budget is None")
        return None, 0.0, "budget_unreadable"
    try:
        sleeve, deployed = await read_budget()
    except Exception as exc:                                           # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        await row("risk", "budget_unreadable", f"the sleeve could not be read ({err}) — the session is refused, "
                  f"NOT allowed", session=session, input="sleeve", error=err)
        return None, 0.0, "budget_unreadable"
    actual = None if sleeve is None else float(sleeve.actual)
    if sleeve is None or actual <= 0.0:
        await row("risk", "budget_unfunded", f"sleeve {'absent' if sleeve is None else f'actual={actual}'} "
                  f"— the session is refused (the sleeve is the sizing basis; an operator exit is `flatten`)", session=session, input="sleeve",
                  value=actual)
        return sleeve, float(deployed), "budget_unfunded"
    return sleeve, float(deployed), None


# `read_equity(broker)` (the ACCOUNT's equity as a sizing basis) was deleted with #986: no caller.
def read_prices(broker, symbols) -> dict[str, float | None]:
    """`broker.last_price(sym)` per symbol; anything unreadable, non-finite or <= 0 is None. WHICH
    price a plan is sized against (last vs prior close vs the fill slot) is the CALLER's convention
    and is NOT decided here — the calling gateway documents the ruling for its lane."""
    out: dict[str, float | None] = {}
    for sym in sorted(symbols):
        try:
            px = broker.last_price(sym)
        except Exception:                                              # noqa: BLE001
            px = None
        try:
            v = float(px) if px is not None else None
        except (TypeError, ValueError):
            v = None
        out[sym] = v if (v is not None and math.isfinite(v) and v > 0) else None
    return out


def plan_is_sells_first(orders) -> tuple[bool, str | None]:
    """(ok, first_buy_symbol). The ordering is the plan's; this only VERIFIES it."""
    seen_buy = None
    for o in orders:
        if o.delta > 0 and seen_buy is None:
            seen_buy = o.symbol
        if o.delta < 0 and seen_buy is not None:
            return False, seen_buy
    return True, seen_buy


def whole_lots(orders, lot: int):
    """The orders whose delta is not a whole number of `lot`. Refused by the caller, never truncated:
    `OrderRequest.qty` is an int and the submit seam is `Quantity.from_int` — the constraint is the
    cockpit's, not the venue's (Alpaca does support fractional shares; nothing here can send one)."""
    return [o for o in orders if float(o.delta) != int(o.delta) or int(o.delta) % int(lot)]


async def execute_plan(orders, *, broker, write_claim, row: RowWriter, sleeve, deployed: float,
                       budget_code: str | None, prices: dict, session: str, slot: str,
                       strategy_id: str, position_side: str = "LONG", limits: dict | None = None,
                       time_in_force: str = "DAY",
                       ) -> tuple[list[dict], dict[str, str], list[str], list[str]]:
    """Submit the plan sequentially. Returns (sent, refused, entered, exited).

    `position_side` DECIDES WHICH VENUE SIDE IS AN ENTRY, and nothing else in here may assume it.
    On a LONG lane a BUY opens and a SELL closes; ON A SHORT LANE THAT IS EXACTLY INVERTED — the
    SELL is the entry. This function used to hardcode the long reading (`if side == "BUY"` for the
    budget gate, `if side == "SELL"` for "exits fund entries"), so wiring a short lane to it would
    have sent every short entry UNGATED and credited it as freeing capital. That is not a small
    error: the budget gate is the only thing standing between a lane and its sleeve.

    ONE DERIVATION, NOT A SECOND COPY. A short-specific submit path would be a second place for
    every rule here to drift — the claim write, the coded refusals, the withholding asymmetry, the
    market-priced decrement. Those are the rules #953/#986/#993 argued over; they are not worth
    having twice.

    `limits` is `{symbol: price}` for a LIMIT order and absent for MARKET. A caller that passes
    nothing keeps the MARKET DAY behaviour byte for byte.

    `time_in_force` APPLIES TO THE WHOLE PLAN and exists so an order that must participate in the
    OPENING CROSS goes through THIS path rather than around it. Submitting such an order directly
    would skip the budget gate, the claim write and the coded refusals — every rule this function
    exists to apply. A non-DAY value also changes the client_order_id, which is what keeps the two
    legs of one intent from colliding in Nautilus' local duplicate check.
    """
    from api.budget_gate import may_submit

    if position_side not in ("LONG", "SHORT"):
        raise ValueError(f"position_side must be LONG or SHORT, not {position_side!r} — this "
                         f"decides which venue side is an ENTRY and must never be defaulted by "
                         f"accident on a lane that shorts")
    long_lane = position_side == "LONG"
    sent: list[dict] = []
    refused: dict[str, str] = {}
    entered: list[str] = []
    exited: list[str] = []
    exit_failed: str | None = None
    for o in orders:
        side = "SELL" if o.delta < 0 else "BUY"
        # THE VENUE SIDE IS NOT THE INTENT. `side` is what the broker is told; `entering` is what it
        # MEANS for this lane, and every gate below keys on the meaning.
        entering = (side == "BUY") if long_lane else (side == "SELL")
        qty = int(abs(o.delta))
        px = prices.get(o.symbol)
        if entering and exit_failed is not None:
            await row("risk", "buy_withheld", f"{o.symbol}: {side} withheld — the exit of "
                      f"{exit_failed} was not accepted and would have funded it", session=session,
                      symbol=o.symbol, after=exit_failed)
            refused[o.symbol] = "buy_withheld"
            continue
        if entering and budget_code is not None:
            refused[o.symbol] = budget_code
            continue
        if entering:
            gate = may_submit(sleeve, is_entry=True, notional=qty * float(px), currently_deployed=deployed)
            if not gate.allowed:
                await row("risk", "budget_refused", f"{o.symbol}: {gate.reason}", session=session,
                          symbol=o.symbol, input="sleeve", notional=qty * float(px), reason=gate.reason,
                          gate=dict(gate.inputs or {}))
                refused[o.symbol] = "budget_refused"
                continue
        code = await _send(o, side, qty, px, session=session, slot=slot, strategy_id=strategy_id,
                           broker=broker, write_claim=write_claim, row=row, sent=sent,
                           limit_px=(limits or {}).get(o.symbol), time_in_force=time_in_force)
        if code is None and not entering and px is not None:
            # SELLS FUND BUYS (#953): an accepted sell frees its notional for the buys that follow.
            # `deployed` was read once and reused for every leg, so a fully deployed sleeve rotating
            # one leg into the other sold, then had its buy `budget_refused` against a room that no
            # longer reflected the book (#986 scope review). ONE BASIS: `deployed` and this decrement
            # are both market-priced (the reader marks claims to the current price, #965); a result
            # below zero means the book was smaller than the sleeve thought — JOURNALED, then clamped,
            # never clamped silently (ffv73l93, #993 review).
            after = deployed - qty * float(px)
            if after < 0.0:
                await row("risk", "deployed_below_zero", f"{o.symbol}: the accepted sell ({qty} × {float(px):,.2f}) "
                          f"exceeds what the sleeve thought was deployed ({deployed:,.2f}) — the book was "
                          f"smaller than recorded; room is clamped at the full sleeve", session=session,
                          symbol=o.symbol, deployed=deployed, freed=qty * float(px), after=after)
            deployed = max(0.0, after)
        if code is not None:
            refused[o.symbol] = code
            # ASYMMETRIC BY DECISION (coordinator, #953 step 7). A refused SELL withholds every later
            # BUY: the sells FUND the buys, so what follows a failed sell is unfundable. A refused BUY
            # does NOT withhold later buys: buys COMPETE for the same cash rather than fund each
            # other, so a smaller later buy may be perfectly fundable after a larger one was refused.
            # Either way PARTIAL names which legs went and which did not.
            if not entering:
                exit_failed = o.symbol
            continue
        (entered if entering else exited).append(o.symbol)
    return sent, refused, entered, exited


async def _send(o, side: str, qty: int, px, *, session: str, slot: str, strategy_id: str, broker,
                write_claim, row: RowWriter, sent: list, limit_px: float | None = None,
                time_in_force: str = "DAY") -> str | None:
    from kumo_strategies.runtime.executor.broker import OrderRequest

    req = OrderRequest(symbol=o.symbol, side=side, qty=qty, session=session, strategy_id=strategy_id,
                       slot=slot or "", limit_px=limit_px, time_in_force=time_in_force)
    try:
        res = broker.submit(req)
    except Exception as exc:                                           # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        await row("error", "submit_raised", f"{o.symbol}: submit raised {err}", session=session,
                  symbol=o.symbol, error=err, client_order_id=req.client_order_id)
        return "submit_raised"
    if not res.ok:
        await row("error", "submit_refused", f"{o.symbol}: {res.detail}", session=session,
                  symbol=o.symbol, reason=res.detail, client_order_id=req.client_order_id)
        return "submit_refused"
    # CLAIM ON ACCEPT, ABSOLUTE, to post_trade_qty — never target_qty, never a delta.
    await write_claim(o.symbol, float(o.post_trade_qty), float(px) if px is not None else None)
    sent.append({"symbol": o.symbol, "side": side, "qty": qty, "delta": float(o.delta),
                 "limit_px": limit_px, "time_in_force": time_in_force,
                 "target_qty": float(o.target_qty), "held_qty": float(o.held_qty),
                 "post_trade_qty": float(o.post_trade_qty), "client_order_id": req.client_order_id,
                 "reason": o.reason})
    return None
