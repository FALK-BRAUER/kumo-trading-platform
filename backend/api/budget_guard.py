"""The per-strategy budget gate, at ONE derivation, for EVERY execution client (#782).

WHY THIS MODULE EXISTS. `may_submit` had exactly one call site — `providers/alpaca/exec_client.py`
— and `providers/alpaca` is the only exec client this repo owns. staging-ibkr runs the SHIPPED
Nautilus IBKR adapter, so on that instance the gate was in nobody's path: BCTROT-004 was allocated
100,000 and reached 118,967.75 because nothing ever checked. `budget.py` and `budget_gate.py` were
fully written and fully tested, and dead.

It was invisible because on the ONE instance that ran the gate, the gate and the intent agreed. That
is the same shape as `KUMO_DATA` (#574/#581) and QC27's hardcoded allocation: agreement is not
connection.

SO THE LOGIC LIVES HERE AND NOTHING COPIES IT. Two exec clients running two copies of a capital
rule would drift, and drift is the subject of half this repo's defects. Both vendors call
`budget_allows` — but ONLY ONE OF THEM SAYS SO IN A GREPPABLE WAY, and that cost a reviewer a wrong
conclusion on 2026-09-11. A plain `grep -rn budget_allows` finds the Alpaca call site and nothing
else, which reads as "the IBKR path is ungated" — the exact defect this module was written to end.
The IBKR path reaches it INDIRECTLY:

    providers/ibkr.py  ExecClientSpec(factory=BudgetGatedIBExecClientFactory)
      -> that factory's `create` calls `install_budget_gate` (providers/gated_exec.py)
      -> which wraps the client's `_submit_order` and calls `budget_allows`.

Named by SYMBOL and not by line number deliberately: the reviewer who hit this quoted a line number
that had already moved between the two tenants' builds, and a stale line in a comment about drift
would be its own joke.

If you are checking whether an instance is gated, do not grep for this function. Read the engine log
for `budget gate INSTALLED` (one line per exec client, at boot), which names the client it wrapped.

NOT A NATIVE MECHANISM, AND THAT WAS CHECKED. Nautilus's `RiskEngineConfig` offers `bypass`,
`max_order_submit_rate`, `max_order_modify_rate` and `max_notional_per_order` — per-ORDER and
per-instrument. There is no native per-strategy CUMULATIVE sleeve, so this control genuinely has to
be ours. (`max_notional_per_order` is worth setting too; it is a different control, not this one.)
"""

from __future__ import annotations

#: How long a loaded sleeve book stays fresh. Reading it per order would put a database call on the
#: submit path of every entry.
BUDGET_TTL_NS = 30 * 1_000_000_000


async def load_book():
    """Read the sleeve book. Plain `await`, no thread handoff.

    An earlier form scheduled onto the loop it was already running on — `_submit_order` is a
    coroutine nautilus creates as a task on that same loop — so it could never complete and timed
    out on every submit.
    """
    from api.budget_store import load_book as _load
    from api.db.engine import session_factory

    async with session_factory() as session:
        return await _load(session)


async def book_for(holder, *, log):
    """The sleeve book for a client, cached on the client for `BUDGET_TTL_NS`.

    SAYS SO WHEN IT CANNOT READ. An earlier version returned the cache silently, and the cache was
    never populated because its only writer sat below a call that always raised — so a gate that
    could not read its book looked exactly like a gate that read an empty one (#642).
    """
    now = int(holder._clock.timestamp_ns())
    cached = getattr(holder, "_budget_cache", None)
    if cached is not None and now - getattr(holder, "_budget_cache_ns", 0) < BUDGET_TTL_NS:
        return cached
    try:
        book = await load_book()
    except Exception as exc:  # noqa: BLE001
        log.error(f"budget book UNREADABLE ({exc!r}) — orders are NOT being checked against a sleeve")
        return None
    holder._budget_cache = book
    holder._budget_cache_ns = now
    return book


async def budget_allows(order, *, cache, book_loader, log, price_of=None, journal=None,
                        max_age_ns: int | None = None, now_ns: int | None = None) -> tuple[bool, str, dict]:
    """May this order proceed under its strategy's budget? -> (allowed, reason, inputs).

    FAILS OPEN, deliberately and unchanged from the Alpaca original. A budget is an ALLOCATION
    POLICY, not a safety interlock — the interlocks are the protective stops and the lifecycle.
    Refusing orders because Postgres hiccuped would stop a strategy trading for a reason that has
    nothing to do with risk, and would do it silently.

    The failure is REPORTED rather than swallowed: `book_for` logs an unreadable book as its own
    condition, so "not checked" cannot read as "checked and fine".

    A MISSING PRICE IS NOT A MISSING BOOK (#990). Until 2026-09-11 a market order with no tick in the
    cache was priced at 0.0, checked at notional 0, and ALLOWED — on staging2 (IBKR, no quote lines
    for the names it trades) the gate had said yes to every entry for two days, silently. Now the
    price comes from `price_of` (default: the lanes' own sequence, `api.last_price.last_price` —
    quote → trade → freshest bar — so the gate and the lane cannot disagree about the number), zero
    and non-finite count as absent, and an absent price REFUSES and names the missing input. Exits are
    exempt BEFORE any pricing, as before: `is_entry` decides, never the price.

    EVERY DECISION LEAVES A TRACE: one log line and, through `journal`, one `exec_action_log` row
    (kind `budget`) carrying room, needs, deployed, the price and its source — allow and refuse alike.
    Refusals were already journaled by the lanes' terminal rows; allows by nobody, so an always-yes
    gate was indistinguishable from an idle one.
    """
    try:
        from api.budget_gate import is_entry as _is_entry
        from api.budget_gate import may_submit

        # AWAITED INSIDE THE TRY, deliberately. Evaluating it at the call site put the book read
        # OUTSIDE the fail-open guard, so an unreadable book raised instead of allowing — a gate
        # becoming an interlock by accident is exactly what the FAILS OPEN note above forbids.
        book = await book_loader()
        if book is None:
            return (True, "", {})
        sleeve = book.sleeves.get(str(order.strategy_id))
        if sleeve is None:
            return (True, "", {})

        net = 0.0
        for pos in cache.positions_open(strategy_id=order.strategy_id,
                                        instrument_id=order.instrument_id):
            net += float(pos.signed_qty)
        qty = float(order.quantity)
        entry = _is_entry(net, order.side.name, qty)
        # the id is a LABEL for the trace, never an input to the decision: an order without one is
        # still checked (the Alpaca-path tests build orders without it, as a lane double might)
        sid, iid = str(order.strategy_id), str(order.instrument_id)
        coid = str(getattr(order, "client_order_id", None) or "?")

        if not entry:
            # EXITS ARE EXEMPT BEFORE ANY PRICING — the documented exemption (a strategy over budget
            # must be able to sell), decided by `is_entry`, never by the price. A SELL that OPENS a
            # short is an entry and is priced below like any other.
            decision = may_submit(sleeve, is_entry=False, notional=0.0, currently_deployed=0.0)
            await _trace(log, journal, allow=True, code="budget_exempt_exit", sid=sid, iid=iid, coid=coid,
                         facts=decision.inputs, why=decision.reason, now_ns=now_ns)
            return (True, decision.reason, decision.inputs)

        # THE PRICE. The order's own limit/trigger where it has one (deliberate: a limit far from the
        # market sizes off the limit — that is the notional the venue can fill); else the lanes' own
        # sequence through `price_of`. Zero and non-finite are ABSENCE. Absence REFUSES.
        px, source, age_ns = None, "order", None
        for attr in ("price", "trigger_price"):
            value = getattr(order, attr, None)
            if value is not None:
                px = float(value); source = f"order.{attr}"
                break
        if px is None or not _usable(px):
            px, source, age_ns = _resolve(cache, order.instrument_id, price_of, max_age_ns, now_ns)
        deployed = 0.0
        for pos in cache.positions_open(strategy_id=order.strategy_id):
            deployed += abs(float(pos.signed_qty)) * float(getattr(pos, "avg_px_open", 0) or 0)
        if not _usable(px):
            why = (f"no price for {iid} — the budget cannot be checked; refusing (missing input: price; "
                   f"tried {source})")
            facts = {"strategy_id": sid, "target": float(sleeve.target), "actual": float(sleeve.actual),
                     "deployed": float(deployed), "room": float(sleeve.deployable(deployed)), "needs": None,
                     "price": "missing", "price_source": source, "qty": abs(qty)}
            await _trace(log, journal, allow=False, code="budget_refused", sid=sid, iid=iid, coid=coid,
                         facts=facts, why=why, now_ns=now_ns)
            return (False, why, facts)
        notional = abs(qty) * px
        decision = may_submit(sleeve, is_entry=True, notional=notional, currently_deployed=deployed)
        facts = dict(decision.inputs or {})
        facts.update({"price": float(px), "price_source": source, "price_age_ns": age_ns, "qty": abs(qty)})
        await _trace(log, journal, allow=bool(decision), code="budget_allow" if decision else "budget_refused",
                     sid=sid, iid=iid, coid=coid, facts=facts, why=decision.reason, now_ns=now_ns)
        return (bool(decision), decision.reason, facts)
    except Exception as exc:  # noqa: BLE001 — see FAILS OPEN above
        log.warning(f"budget check skipped ({exc!r}) — allowing")
        return (True, "", {})


def _usable(px) -> bool:
    import math
    return px is not None and math.isfinite(px) and px > 0.0


def _resolve(cache, instrument_id, price_of, max_age_ns, now_ns):
    """(price, source, age_ns) from the injected resolver or the lanes' own sequence."""
    if price_of is not None:
        try:
            v = price_of(instrument_id)
        except Exception as exc:                                       # noqa: BLE001
            return None, f"price_of:raised({type(exc).__name__})", None
        return (float(v) if v is not None else None), "price_of", None
    from api.last_price import last_price
    got = last_price(cache, instrument_id, max_age_ns=max_age_ns, now_ns=now_ns)
    return got.price, got.source, got.age_ns


def _session_key() -> str:
    """The lanes journal by ET trading date (ISO); the gate's row uses the same key."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


async def _trace(log, journal, *, allow: bool, code: str, sid: str, iid: str, coid: str, facts: dict,
                 why: str, now_ns) -> None:
    """One log line AND one journal row per decision. Neither may raise into the trading path."""
    verdict = "ALLOW" if allow else "REFUSE"
    line = (f"budget {verdict} {coid} {sid} {iid}: room={facts.get('room')} needs={facts.get('needs')} "
            f"deployed={facts.get('deployed')} price={facts.get('price')} ({facts.get('price_source', 'n/a')})"
            + (f" — {why}" if why else ""))
    try:
        (log.info if allow else log.warning)(line)
    except Exception:                                                  # noqa: BLE001
        pass
    if journal is None:
        return
    try:
        await journal(kind="budget", code=code, strategy_id=sid, symbol=iid, summary=line[:200],
                      detail=dict(facts), session=_session_key(), log=log)
    except Exception as exc:                                           # noqa: BLE001
        try:
            log.error(f"budget journal row NOT written ({code} {sid} {iid}): {exc!r} — no durable trace")
        except Exception:                                              # noqa: BLE001
            pass


#: The gate's default price resolver: the LANES' sequence. The identity the tests pin: when
#: kumo_strategies exposes `last_price_from_cache`, THAT is the default and the cockpit mirror is deleted.
def _default_price_of():
    try:
        from kumo_strategies.runtime.nautilus import broker as _upstream
        fn = getattr(_upstream, "last_price_from_cache", None)
        if fn is not None:
            return fn
    except Exception:                                                  # noqa: BLE001
        pass
    from api.last_price import last_price
    return last_price


DEFAULT_PRICE_OF = _default_price_of()
