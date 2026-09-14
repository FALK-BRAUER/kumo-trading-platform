"""SMHGLD — the fixed-weight SMH/GLD sleeve's session gateway: delta execution (kumo-cockpit#953).

WHAT THIS MODULE IS. The cockpit half of kumo-strategies#177. The adapter (`runtime/nautilus/
smhgld_sleeve.py`) calls `runner.run(panel, session, slot=...)` once per decision slot; this
gateway turns the lane's `order_plan()` — a TARGET and a DELTA per symbol — into at most one order
per symbol per session, sells first, claims set absolutely, and refuses by name whatever it cannot
size or send. The strategy logic lives upstream (`kumo_strategies.strategies.smhgld_sleeve`);
nothing here decides, targets, rounds or orders.

THE BOUNDARY (Operator, 2026-09-11: "double fix then" — the executing half is LANE-AGNOSTIC):
  * `strategies/delta_execution.py` EXECUTES a plan: sequential submission in the plan's order,
    sells fund buys (a refused SELL withholds later BUYs), buys compete (a refused BUY does not),
    buys budget-gated and sells never, the claim set ABSOLUTELY to `post_trade_qty`, every refusal
    coded. It takes `tuple[Order, ...]`, a submit seam, a claim writer, a sleeve and prices, and
    knows NOTHING about which lane produced them. ks#184 (`rebalance_band` on momentum) is its
    SECOND consumer, a wiring change.
  * THIS module holds SMHGLD's POLICY: its symbols (injected, never learned by the executor),
    "no flat state", the protection stance (registry, #1029), the capability declaration, and the journal shape.

THE CONTRACT THIS CONSUMES (l21, read from the code at f36b674, never from prose):

    order_plan(decision, shares, prices, equity, *, lot=1.0) -> tuple[Order, ...]
    Order(symbol, delta, target_qty, held_qty, reason); Order.post_trade_qty == held_qty + delta

    * sorted SELLS FIRST already (most negative delta first)
    * deltas already rounded to a whole lot, TOWARD the current holding
    * a zero delta is ABSENT; an empty tuple is the normal quiet-week answer — a REAL decision
    * regime not in {opening, rebalance} -> () — also a real decision
    * an entry is a delta from zero and an exit is a target of zero: the words `enter`/`exit`
      never appear, and a runner that looks for them executes NOTHING while the journal fills.

THE CAPABILITY IS AN IDENTITY, NOT A NAME. The strategies-side gate (l21) refuses to register a
lane against a runner that does not list the `EXECUTES_DELTAS` token BY IDENTITY in `EXECUTES` —
a name is satisfied by anything that spells it (cockpit#955's shape). The token's `means` includes
the ordering ("SELLS BEFORE BUYS"), so declaring it declares both; `plan_unordered` below is the
complement (a plan that arrives wrong is refused), not a duplicate. On a pin without the token the
tuple is EMPTY and the gate refuses — correct, since no SMHGLD lane exists on that pin.

FIVE THINGS NOT RE-DERIVED HERE: the ordering (verified, never re-sorted — refused whole), the
rounding (a fractional delta is REFUSED, never truncated: `OrderRequest.qty` is an int and the one
submit seam is `Quantity.from_int`, so lot=1 is the COCKPIT's constraint on both venues — Alpaca
itself supports fractional shares; the constraint is ours, asserted, not assumed), the target, the
delta, and the claim's value (`post_trade_qty`, NEVER `target_qty`).

THE PRICE CONVENTION IS NOT DECIDED HERE (l21; coordinator ruling pending): the adapter decides at
`open+5m` while the config's only measured fill slot is `close`, and `order_plan` was never compared
against the research simulation. Prices come through an injectable reader (`read_prices`, default
`broker.last_price`) so the ruled convention is wired by the builder, not baked in.

NO FLAT STATE (policy, `no_flat_state=True`). This lane enters once and never exits; a weight
symbol held at zero that the session's plan does not open gets an `incident` row (`no_position`).
That is the DECISION-TIME half; the continuous half (a hole opened between decisions by a stop-out,
a flatten, a corporate action or a reconciliation flatting order) is kumo-cockpit#961 — the
continuous reconciler reads flat as resting and must read the lane's DECLARED holdings instead.

PROTECTION: NONE, BY DECLARATION (coordinator's ruling, #953) — DECLARED IN THE REGISTRY, NOT HERE
(#1029). A stop sized to the whole holding would be tripped by the lane's own trims — the PEAK
cancel-and-replace class that stripped protection off five live positions on 2026-08-12 — and the
measured drawdown already includes having no stop. The stance lives on `StrategyEntry.protection`
(`api/strategy_registry.py`), which is what `api.protection.lane_modes` reads when the tenant's
file is silent; the settings key `<lane>_protection` is the operator OVERRIDE. This module used to
carry `PROTECTION_MODE = "none"` and `PROTECTION_REASON` — read by nothing, while both tenants
resolved the lane to TRAIL. One derivation means one: a constant here would be a second copy that
merely agrees, which is the shape that produced the defect. With no stops, the #873 hooks and the
operator flatten are the entire safety surface — and AN EMERGENCY EXIT OF THIS SLEEVE IS UNDONE BY
ITS NEXT DECISION (lab's live note): flatten it by hand and `decide()` returns "opening" and buys
both legs back next session. To STOP the lane, halt or deregister it; a flatten alone does not.

CAPABILITY, NOT DEPLOYMENT. No registry entry, no builder, no settings key, no pin, no instance
here (#953 scope). The lane is registered nowhere and that is the correct state until the
strategies-side gate and #961 land and this is wired.
"""
from __future__ import annotations

import logging

from strategies.delta_execution import (execute_plan, plan_is_sells_first, read_prices, read_sleeve,
                                        whole_lots)

_log = logging.getLogger("kumo.strategies.smhgld")

#: The default universe; the builder passes the adapter's weights' symbols explicitly.
DEFAULT_SYMBOLS = ("SMH", "GLD")

# THE IDENTITY TOKEN IS UPSTREAM (l21's gate, ks#185, merged 2026-09-11 into 06f3055) and this gateway
# REQUIRES it — the import sits in the CLASS BODY below: kumo_strategies may not be imported at module
# scope in a production module (api/test_claims_invariant_without_the_pin.py), and the try/except that
# stood here until 06f3055 (`_EXECUTES = ()` on an older pin) was the banned silent-fallback shape —
# absence rendered as a valid declaration. A pin without the token now fails the class definition by name.

_POSITION_SIDE = "LONG"


def read_prices_default(broker, symbols) -> dict[str, float | None]:
    """`broker.last_price` — the DEFAULT, not the ruling; see the module docstring."""
    return read_prices(broker, symbols)


class SmhgldSessionGateway:
    """The session runner the SMHGLD adapter calls. `journal` is a PUBLIC attribute under that exact
    name — the adapter's `session_journal()` reads it there (#587)."""

    #: The capability, declared by IDENTITY (see the module docstring).
    from kumo_strategies.runtime.nautilus.capabilities import EXECUTES_DELTAS as _EXECUTES_DELTAS
    EXECUTES = (_EXECUTES_DELTAS,)
    del _EXECUTES_DELTAS

    def __init__(self, *, journal, broker, read_state, plan, strategy_id: str,
                 read_budget=None, write_claim=None, lot: int = 1, symbols=DEFAULT_SYMBOLS,
                 no_flat_state: bool = True, read_prices=None):
        self.journal = journal
        self._broker = broker
        self._read_state = read_state
        #: `(panel, session, *, held, prices, equity, lot) -> (decision_view, tuple[Order, ...])`,
        #: `decision_view = {"regime", "weights"}` read off the adapter's `Decision` (l21: name the
        #: two fields, never journal the whole dataclass — a grown field is a schema change nobody
        #: decided). The builder composes it from the adapter's cfg, `decide()` and `order_plan()`.
        self.plan = plan
        self.strategy_id = strategy_id
        self.read_budget = read_budget
        #: `async (symbol, absolute_qty, px) -> None`: `sync_claim_to` in the builder. A precondition
        #: of SENDING: an accepted order with no claim is a position every neighbour can reach into.
        self.write_claim = write_claim
        self.lot = int(lot)
        #: The lane's universe — what is priced and what "no flat state" applies to. Injected, so the
        #: executing half never learns a symbol.
        self.symbols = tuple(symbols)
        self.no_flat_state = bool(no_flat_state)
        #: `(broker, symbols) -> {symbol: price | None}`; the price CONVENTION is the builder's.
        self.read_prices = read_prices if read_prices is not None else read_prices_default
        self._slot: str | None = None
        self._journal_failures = 0

    async def _row(self, kind: str, code: str, summary: str, *, session: str, symbol=None, **detail):
        row_id = await self.journal.write(kind, summary, session=session, slot=self._slot,
                                          symbol=symbol, detail={"code": code, **detail})
        if row_id is None:
            self._journal_failures += 1
        return row_id

    async def run(self, panel, session: str, jobs=None, slot: str | None = None):
        """One session for one slot. `jobs` and `slot` are the SessionRunner protocol; `jobs` is
        unused (this lane's universe is its weights)."""
        from kumo_strategies.runtime.executor.runner import SessionResult

        if slot:
            self._slot = slot
        self._journal_failures = 0
        life = await self._read_state()
        state = life.state

        def _result(**kw):
            return SessionResult(session=session, state=state.value, **kw)

        if not state.decides:
            return _result(decided=False, blocked=f"lifecycle {state.value}")
        if await self.journal.decided_this_session(session, self._slot):
            return _result(decided=True, submitted=0, blocked="already decided this slot (resume)")
        if state.may_submit_exits and not state.may_submit_entries:
            why = (f"lifecycle {state.value}: this gateway sends nothing while the lane is being wound "
                   f"down — liquidate_lane (kumo-cockpit#922) owns it")
            await self._row("risk", "liquidating", why, session=session, lifecycle=state.value,
                            mechanism="liquidate_lane", issue="kumo-cockpit#922")
            return _result(decided=True, submitted=0, blocked=why)

        acting = state.may_submit_entries
        if acting and self.write_claim is None:
            why = ("no claim writer is wired — an accepted order with no claim is an unclaimed "
                   "position every neighbour can reach into; the whole session is refused")
            await self._row("risk", "claims_unwired", why, session=session)
            return _result(decided=False, blocked=why)

        # --- inputs, read ONCE and refused by name; the plan is not asked from a bad input --------
        # THE SLEEVE IS THE EQUITY (#986). The first version sized targets off `read_equity(broker)` —
        # the ACCOUNT's net liquidation (Alpaca `portfolio_value`; on IB the base-currency NLV) — and
        # only afterwards let `may_submit` REFUSE buys over the sleeve: a 100k account with a 20k sleeve
        # planned 32k/69k and opened nothing, with coded rows and every surface green. The account
        # number is not an input to this lane; it appears nowhere below. The basis is the GATE'S OWN
        # derivation, `Sleeve.deployable(0.0)` = min(actual, target), so sizing and gating cannot
        # disagree. Read BEFORE the plan: an unreadable or unfunded sleeve refuses the WHOLE session —
        # sells included, because a sell sized from a bogus equity is a bogus trim, not a safe exit.
        # THE ESCAPE HATCH IS FLATTEN, and it is independent of this read: `_handle_flatten_command`
        # takes instrument, strategy, side and quantity off the live book and never touches
        # `strategy_sleeve`, `budget_store` or this lane's equity (driven on an SMHGLD-shaped holding,
        # LONG 130 → SELL 130). Do not add a partial path here; it would reintroduce the defect wearing
        # the word "sell".
        sleeve, deployed, budget_code = await read_sleeve(self.read_budget, self._row, session=session)
        if budget_code is not None:
            return _result(decided=False, blocked=f"{budget_code} — the sleeve is the sizing basis; "
                                                  f"an operator exit is `flatten`, which does not read it")
        try:
            book = {str(k): v for k, v in dict(self._broker.strategy_positions()).items()}
        except Exception as exc:                                       # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            await self._row("error", "book_unreadable", f"held-book read failed: {err} — the session "
                            f"is refused; a delta from an invented book is a trade", session=session,
                            error=err)
            return _result(decided=False, blocked=f"book unreadable ({err})")
        prices = self.read_prices(self._broker, set(book) | set(self.symbols))
        # BOTH SIDES OF THIS CONFLICT ARE KEPT, AND THE ORDER IS THE POINT (#985 x #992/#993).
        # The price refusal runs FIRST: the reserve below does `prices.get(s) is not None`, so a leg
        # with no price would be silently DROPPED from `leg_prices` and the reserve taken off the one
        # remaining leg — a smaller reserve, computed from half the book, on exactly the input that
        # cannot be sized at all. Refusing the session above makes every symbol in `self.symbols`
        # priced by the time the reserve is computed, so `leg_prices` covers every leg by
        # construction rather than by luck.
        # BEFORE THE PLAN, NOT AFTER (l21, #985 review). `order_plan` indexes `prices[symbol]` with no
        # guard, so an absent key raises KeyError and a None value raises TypeError — both INSIDE the
        # broad `except` below, which codes them `plan_failed`. The refusal written for exactly this
        # case was therefore unreachable, and the operator got `plan failed: KeyError: 'GLD'` instead
        # of a sentence naming the missing input. `missing` needs only `prices` and this lane's own
        # symbols, both of which exist here; the held book's names are checked too, since a leg we
        # hold and cannot price cannot be sized either.
        missing = sorted(sym for sym in set(self.symbols) | set(book) if prices.get(sym) is None)
        for sym in missing:
            await self._row("risk", "price_missing", f"{sym}: no price — a target computed from it is "
                            f"not a size; the whole session is refused (the other leg must not trade "
                            f"alone)", session=session, symbol=sym, input="price", value=None)
        if missing:
            return _result(decided=False, blocked=f"price missing for {missing}")
        # ONE SHARE OF THE PRICIEST LEG IS RESERVED (#992, ruled 2026-09-11). Rounding toward the
        # holding makes a SELL leave MORE than target and a BUY take LESS, so the residuals have
        # opposite signs and the post-trade book overshoots the basis whenever the sell leg's residual
        # exceeds the buy leg's (measured: GLD +348, SMH −180 → +168 over 20,000; the buy refused,
        # every session, forever). The residual cannot exceed one share of any single leg, so sizing
        # against `basis − max(price over the legs)` bounds it by construction — deterministic,
        # self-scaling (2.8% at 20k, 0.19% at 300k), read from the plan's own prices at sizing time,
        # never a constant (SMH doubled the number estimated this morning). The gate stays hard at
        # the basis; the rounding stays toward the holding; only the sizing target moves. A cockpit
        # workaround: the real fix is upstream (kumo-strategies#186 — `order_plan` rounding buys
        # toward the sleeve), and this reserve leaves with it. A NOTE THAT STOOD HERE WAS FALSE and is
        # recorded rather than deleted: it said staging2's sleeves all read actual=0 and that this
        # gateway would refuse every session there. Measured from the running instance (`load_book`
        # inside kumo-staging2-api-1, 2026-09-11 11:46Z): all six lanes are target=100,000.00 and
        # actual=100,000.00, deployable(0)=100,000.00, is_reducing=False. The `strategy_sleeve.target`
        # COLUMN reads 0 there and is vestigial — targets come from the settings domain — and a
        # headerless `select *` read column-order backwards. The basis on staging2 is 100,000, not 0.
        import math
        basis = float(sleeve.deployable(0.0))
        if not math.isfinite(basis) or basis <= 0.0:
            # NON-FINITE IS NOT A BASIS. `nan <= 0.0` is False and `max(0.0, nan)` happens to be 0.0
            # today, so a NaN `actual` lands here by accident of `deployable`'s clamp; the deleted
            # `read_equity` carried the only `isfinite` check. Explicit, so it does not depend on that.
            # A ZERO BASIS WITH CAPITAL IN THE SLEEVE (target 0, actual > 0) is not "unfunded": it is
            # a wind-down intent, and sizing off it would target 0 shares on every leg — SELL
            # EVERYTHING, inferred (ffv73l93, #993 review). A wind-down is an operator FLATTEN.
            # Refused by its own code, with both numbers on the row, before any plan is asked.
            await self._row("risk", "sleeve_basis_zero", f"the sleeve's sizing basis min(actual, target) is "
                            f"{basis:,.2f} (target {float(sleeve.target):,.2f}, actual {float(sleeve.actual):,.2f}) "
                            f"— nothing is sized; a wind-down is an operator flatten, never an inferred liquidation",
                            session=session, input="sleeve", target=float(sleeve.target), actual=float(sleeve.actual))
            return _result(decided=False, blocked="sleeve_basis_zero — an operator exit is `flatten`, which does not read the sleeve")
        leg_prices = [float(prices[s]) for s in self.symbols if prices.get(s) is not None]
        reserve = max(leg_prices) if leg_prices else 0.0
        equity = basis - reserve
        if equity <= 0.0:
            await self._row("risk", "budget_unfunded", f"the sleeve's capital ({float(sleeve.deployable(0.0)):,.2f}) "
                            f"does not cover one share of the priciest leg ({reserve:,.2f}) — nothing can "
                            f"be sized", session=session, input="sleeve", value=float(sleeve.deployable(0.0)),
                            reserve=reserve)
            return _result(decided=False, blocked="budget_unfunded — the sleeve is smaller than one share; "
                                                  "an operator exit is `flatten`, which does not read it")
        try:
            decision_view, orders = self.plan(panel, session, held=dict(book), prices=prices,
                                              equity=equity, lot=self.lot)
        except Exception as exc:                                       # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            await self._row("error", "plan_failed", f"order_plan failed: {err}", session=session, error=err)
            return _result(decided=False, blocked=f"plan failed: {err}")
        weights = dict((decision_view or {}).get("weights") or {})
        regime = (decision_view or {}).get("regime")
        # A WEIGHT ON A SYMBOL THIS LANE DOES NOT LIST is checked here because only the plan can
        # introduce one; the lane's own legs and its held book were checked above, before the plan.
        unpriced = sorted(sym for sym in weights if prices.get(sym) is None)
        for sym in unpriced:
            await self._row("risk", "price_missing", f"{sym}: the plan weights a symbol with no price "
                            f"— a target computed from it is not a size; the whole session is refused",
                            session=session, symbol=sym, input="price", value=None)
        if unpriced:
            return _result(decided=False, blocked=f"price missing for {unpriced}")
        orders = tuple(orders)

        # --- the plan, verified, never re-derived ---------------------------------------------
        ok, first_buy = plan_is_sells_first(orders)
        if not ok:
            await self._row("risk", "plan_unordered", f"the plan is not sells-first (first buy "
                            f"{first_buy} precedes a sell) — refused whole; the ordering is the plan's "
                            f"and is not re-derived here", session=session, first_buy=first_buy,
                            order=[(o.symbol, float(o.delta)) for o in orders])
            return _result(decided=False, blocked="plan not sells-first")
        fractional = whole_lots(orders, self.lot)
        for o in fractional:
            await self._row("risk", "fractional_delta", f"{o.symbol}: delta {o.delta} is not a whole "
                            f"lot of {self.lot} — refused, never truncated (OrderRequest.qty is an "
                            f"int; the submit seam is Quantity.from_int)", session=session,
                            symbol=o.symbol, delta=float(o.delta), lot=self.lot)
        if fractional:
            return _result(decided=False, blocked="fractional delta in the plan")

        # --- no flat state (policy): a weight symbol held at zero that this plan does not open ---
        if self.no_flat_state:
            planned = {o.symbol for o in orders}
            for sym, w in sorted(weights.items()):
                held = float(book.get(sym, 0) or 0)
                if held == 0 and w > 0 and sym not in planned:
                    await self._row("incident", "no_position", f"{sym}: the design holds {w:.0%} and "
                                    f"the lane holds NOTHING, and this session's plan does not open it — "
                                    f"there is no flat state on this lane; this is an incident, not a "
                                    f"resting state (a flatten is undone by the next decision: halt or "
                                    f"deregister to stop the lane)", session=session, symbol=sym,
                                    held=0, weight=w, regime=regime)

        if not acting:
            detail = self._detail(orders, regime, weights, [], {}, book, equity, shadow=True)
            await self.journal.write("decision", self._summary(session, orders, [], {}, regime,
                                                               "SHADOW, nothing sent"),
                                     session=session, slot=self._slot, detail=detail)
            return _result(decided=True, submitted=0, detail=detail)

        sent, refused, entered, exited = await execute_plan(
            orders, broker=self._broker, write_claim=self.write_claim, row=self._row, sleeve=sleeve,
            deployed=deployed, budget_code=budget_code, prices=prices, session=session,
            slot=self._slot or "", strategy_id=self.strategy_id)

        partial = bool(refused) and bool(orders)
        detail = self._detail(orders, regime, weights, sent, refused, book, equity, shadow=False)
        row_id = await self.journal.write("decision", self._summary(session, orders, sent, refused, regime,
                                                                    "PARTIAL" if partial else state.value),
                                          session=session, slot=self._slot, detail=detail)
        detail["decision_durable"] = row_id is not None
        blocked = None
        if partial:
            blocked = (f"PARTIAL: sent {[s['symbol'] for s in sent]}, refused {refused} — the plan was "
                       f"not completed this session")
        elif row_id is None:
            blocked = f"decision row NOT durable (journal returned None) after {len(sent)} order(s)"
        return _result(decided=True, submitted=len(sent), entered=tuple(entered), exited=tuple(exited),
                       held=tuple(sorted(k for k, v in book.items() if v)), detail=detail, blocked=blocked)

    def _detail(self, orders, regime, weights, sent, refused, book, equity, *, shadow: bool) -> dict:
        # `lot` and `equity` travel with the weights: a target is `weight * equity / price`, and a
        # row that records the weights but not the equity cannot be re-derived by its reader (l21).
        return {"position_side": _POSITION_SIDE, "regime": regime, "weights": dict(weights),
                "equity": float(equity), "lot": self.lot,
                "orders": [{"symbol": o.symbol, "delta": float(o.delta), "target_qty": float(o.target_qty),
                            "held_qty": float(o.held_qty), "post_trade_qty": float(o.post_trade_qty),
                            "reason": o.reason} for o in orders],
                "held": {k: v for k, v in sorted(book.items()) if v}, "shadow": shadow,
                "sent": sent, "gateway_refused": refused}

    @staticmethod
    def _summary(session, orders, sent, refused, regime, tail) -> str:
        return (f"SMHGLD {session}: plan {len(orders)} orders (regime {regime}) · sent "
                f"{[s['symbol'] for s in sent]} · refused {refused}"
                f"{' · 0 orders: quiet session, a real decision' if not orders else ''} — {tail}")


# -- registration (#965): settings gate, production callables, the builder -------------------------------

STRATEGY_ID = "SMHGLD-007"
ORDER_ID_TAG = "007"
#: Registration writes a SHADOW row when none exists (CRSISHORT's rule, codex #858 review): an operator
#: moves it to TRADING deliberately. An absent row would read TRADING (Operator, 2026-08-19).
_REGISTRATION_STATE = "SHADOW"


def _settings() -> dict:
    from api import settings

    return settings.resolve("strategies") or {}


def _enabled() -> bool:
    """From SETTINGS, not the environment — registering a strategy is an operator decision."""
    return bool(_settings().get("SMHGLD_ENABLED"))


def _symbols() -> tuple[str, str]:
    raw = _settings().get("SMHGLD_SYMBOLS") or list(DEFAULT_SYMBOLS)
    # No count check here: the builder compares the tuple against the upstream config's two legs,
    # and a wrong count is a mismatch like any other (a separate count guard was dead code — bitten).
    return tuple(str(x).strip().upper() for x in raw)


def production_plan(cfg):
    """The gateway's `plan`: the UPSTREAM `decide` and `order_plan`, never a re-derivation.

    THE GATEWAY'S CALL, matched exactly (seam test drives the real `run()`):
    `(panel, session, *, held, prices, equity, lot) -> (decision_view, orders)` where `held` is the
    lane's OWN book as `{symbol: signed_qty}` — the upstream `decide` takes the held SET and the
    shares dict from that one input. The view is the two fields the journal names (`regime`,
    `weights`), read off the adapter's `Decision`.
    """
    from kumo_strategies.strategies.smhgld_sleeve.engine import decide, order_plan

    def _plan(panel, session, *, held, prices, equity, lot=1):
        shares = {str(k): float(v) for k, v in dict(held or {}).items() if v}
        decision = decide(panel, cfg, set(shares), shares)
        orders = order_plan(decision, shares, dict(prices), float(equity), lot=float(lot))
        return {"regime": decision.regime, "weights": dict(decision.weights)}, tuple(orders)

    return _plan


async def _ensure_registration_row(sm) -> str:
    from kumo_strategies.runtime.executor.store import StrategyState, select
    async with sm() as s:
        row = (await s.execute(select(StrategyState).where(
            StrategyState.strategy_id == STRATEGY_ID))).scalars().first()
        if row is not None:
            return str(row.state)
        s.add(StrategyState(strategy_id=STRATEGY_ID, state=_REGISTRATION_STATE,
                            reason="registered by build_smhgld_strategy (#965): SHADOW until an operator moves it"))
        await s.commit()
        return _REGISTRATION_STATE


def build_smhgld_strategy(*, feed=None):
    """SMHGLD-007 wired to `SmhgldSessionGateway`, or None when the gate is off.

    RAISES when the gate is ON but a prerequisite is missing, rather than returning None — a lane
    that is switched on and silently absent is the worst outcome available. `build_optional_strategy`
    in `engine_node` separates that from transient transport failure by exception TYPE. The one
    invariant: `session_runner` is ALWAYS passed — the upstream adapter's capability guard refuses a
    runner that cannot execute deltas, and without a runner it would decide and submit on its own.
    """
    if not _enabled():
        return None
    symbols = _symbols()
    import asyncio
    from kumo_strategies.runtime.calendar import build_calendar
    from kumo_strategies.runtime.executor.pgjournal import PgJournal
    from kumo_strategies.runtime.executor.store import create_all, make_engine, make_sessionmaker
    from kumo_strategies.runtime.nautilus.broker import NautilusBroker

    async def _prepare():
        eng = make_engine(None, cross_loop=True)
        await create_all(eng)
        sm = make_sessionmaker(eng)
        try:
            await _ensure_registration_row(sm)
        except Exception as exc:                                       # noqa: BLE001
            _log.error("%s: could not write the registration lifecycle row (%r) — the lane reads "
                       "TRADING (absent row)", STRATEGY_ID, exc)
        return sm

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        sm = asyncio.run(_prepare())
    else:  # pragma: no cover — build runs before the node's loop exists
        raise RuntimeError(
            "build_smhgld_strategy must run before the node's event loop starts — it opens a "
            "cross-loop engine for the lifecycle and journal stores")
    try:
        from kumo_strategies.runtime.nautilus.smhgld_sleeve import SmhGldSleeveStrategy
        from kumo_strategies.strategies.smhgld_sleeve.config import SmhGldSleeveConfig
    except ImportError as exc:
        raise RuntimeError(
            f"strategies.SMHGLD_ENABLED is on but the installed kumo-strategies has no SMHGLD sleeve "
            f"adapter ({exc}) — the pin predates kumo-strategies#177 (06f3055). Unset the gate or "
            f"move the pin; the lane cannot register on this revision.") from exc
    cfg = SmhGldSleeveConfig()
    if (cfg.risk_asset, cfg.defensive_asset) != symbols:
        raise RuntimeError(
            f"strategies.SMHGLD_ENABLED is on but strategies.SMHGLD_SYMBOLS={symbols} does not match "
            f"the upstream config ({cfg.risk_asset!r}, {cfg.defensive_asset!r}) — exactly two legs, "
            f"risk asset then defensive asset; the weights are defined for those. Change the config "
            f"upstream, not the legs here.")

    async def _read_state():
        """A FRESH lifecycle read per session; an absent row is TRADING (the rule every gateway
        applies), which is why registration writes SHADOW first."""
        from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State
        from kumo_strategies.runtime.executor.store import StrategyState, select
        async with sm() as s:
            row = (await s.execute(select(StrategyState).where(
                StrategyState.strategy_id == STRATEGY_ID))).scalars().first()
        return (Lifecycle(State(row.state), row.reason) if row
                else Lifecycle(State.TRADING, "default: no lifecycle row"))

    async def _read_budget():
        """(sleeve, deployed): the sleeve from the budget book (target from settings, actual from
        the table); deployed = what THIS lane's claims say it holds, MARKED TO THE CURRENT PRICE
        through the gateway's own price seam (l21, #985 review: at ENTRY a leg up 40% reads as
        under-deployed and the budget lets the lane buy more of it; every other budget reader marks
        to the current price). A missing price RAISES — UNREADABLE at the executor, buys refused —
        never a fallback to the entry price, which would be the same defect one level down."""
        from api.budget_store import load_book
        from kumo_strategies.runtime.executor.store import PositionState, select
        async with sm() as s:
            book = await load_book(s)
            rows = (await s.execute(select(PositionState).where(
                PositionState.strategy_id == STRATEGY_ID))).scalars().all()
        held = {str(r.symbol): abs(float(r.qty)) for r in rows if r.qty}
        prices = read_prices_default(broker, set(held))
        missing = sorted(sym for sym in held if prices.get(sym) is None)
        if missing:
            raise RuntimeError(f"no current price for {missing} — deployed cannot be marked; "
                               f"buys refused rather than priced at entry")
        deployed = sum(q * float(prices[sym]) for sym, q in held.items())
        return book.sleeves.get(STRATEGY_ID), float(deployed)

    async def _write_claim(symbol: str, post_trade_qty: float, px):
        """The claim SET to the post-trade quantity (#953's absolute-set rule), through the one
        predicate every gateway shares (#950)."""
        from api.claims_sync import sync_claim_from_book
        await sync_claim_from_book(journal, STRATEGY_ID, symbol, int(post_trade_qty), px)

    from strategies.decision_slots import BUILTIN_SLOTS
    from strategies.momentum import _read_slots_for
    from strategies.slot_reader import _live_reread_kwargs
    journal = PgJournal(sm, strategy_id=STRATEGY_ID)
    broker = NautilusBroker(strategy=None, instrument_ids=None)   # resolved by the strategy (#622)
    gateway = SmhgldSessionGateway(
        journal=journal, broker=broker, read_state=_read_state, plan=production_plan(cfg),
        strategy_id=STRATEGY_ID, read_budget=_read_budget, write_claim=_write_claim,
        symbols=symbols)
    slots = BUILTIN_SLOTS[STRATEGY_ID]   # declared, never defaulted: a missing entry is a KeyError at build
    strategy = SmhGldSleeveStrategy(
        cfg,
        symbols=list(symbols),
        order_id_tag=ORDER_ID_TAG,
        calendar=build_calendar(
            require_exchange=True,
            calendar=getattr(feed, "_venue_calendar", None),
        ),
        session_runner=gateway,
        decision_slots=slots,
        # HISTORY, OR THE LANE CAN NEVER BE WARM (kumo-strategies#200). `on_start` subscribes to
        # FUTURE bars; `subscribe_bars` takes no start and no lookback, so it never backfills. The
        # lane's `warm` gate needs `max(warmup_sessions, 2)` = 3 DAILY bars on BOTH legs, and a 1-DAY
        # subscription delivers one per leg per session — three trading days before it could decide.
        # Measured live on staging2 2026-09-11: SMHGLD armed, fired its rung, and returned silently
        # because `not self.warm`, with zero bars for its own universe.
        #
        # THROUGH `_live_reread_kwargs` AND NOT AS A LITERAL KWARG, because `history_days` arrives
        # with ks#200 and this repo pins by revision: passing it to an adapter that predates it
        # raises TypeError at BUILD and takes every other lane down with it. The helper passes only
        # what the INSTALLED class accepts, so an older pin degrades to today's behaviour instead of
        # failing to boot.
        #
        # 90 CALENDAR DAYS, BECAUSE THE VIEW READS THESE BARS TOO (#1049). The floor used to be the
        # lane's own `_need` (3 sessions) and 30 days covered it with slack. ks#212 makes the lane
        # declare a 50-session market view, and `MarketAwareMixin._view_prices` reads the SAME
        # per-leg bar container `on_start` fills from this request — so the requirement is
        # `max(view.window + 1, _need)` = 51 SESSIONS, not 3. 30 days is 18-21 sessions: the view
        # would read UNKNOWN until late October and `hook_unknown_twice` would page on every restart
        # until then (measured 2026-09-12: a 10:03Z boot requested `start=2026-08-13`).
        #
        # 90 and not 75 (lead's ruling 2026-09-13): the requirement is in SESSIONS and this knob is in
        # CALENDAR DAYS; a 75-day window ending in early January bounds to 49 sessions. The test
        # derives the bound from the date range on every boot date of the coming year and never
        # asserts this literal — change the number and it says whether the new one still covers.
        # The ks half (`_bars` sized to the view, ks#212) must land in the same deploy; this request
        # alone fills a container that discards everything past `_need + 5`.
        **_live_reread_kwargs(
            SmhGldSleeveStrategy,
            read_slots=lambda: _read_slots_for(STRATEGY_ID, slots),
            history_days=90),
    )
    from api.venue_preference import prefer_primary_exchange
    strategy._prefer_venue = lambda sym, cands, _s=strategy: prefer_primary_exchange(_s.cache, sym, cands)
    broker.strategy = strategy
    broker.feed = feed
    return strategy
