"""TECHIVOL-005 — QC27 Tech Momentum with Inverse Volatility, hosted by the engine node.

WHY THIS MODULE IS THIN, and qc345.py is 770 lines. QC345's session gateway lives HERE because
`PgSessionRunner` was momentum-specific and could not be reused, so cockpit had to grow its own.
QC27 does not: `kumo_strategies.runtime.executor.qc27_runner.QC27SessionRunner` already owns the
order of operations — idempotency, lifecycle, journal-before-broker, the last look, exits before
entries — and is tested and mutation-bitten in the repo that defines those controls.

So this module supplies ONLY what that runner deliberately refuses to guess, because guessing would
make it a second derivation of something cockpit owns:

    read_state / recheck_state   the operator's lifecycle, read fresh EVERY session
    budget_gate                  cockpit's own `may_submit`, not a second budget rule
    limits.allocated_equity      THIS STRATEGY's 20k, never the account's equity
    journal                      PgJournal bound to TECHIVOL-005
    cfg                          the promoted config, from settings

THE ONE INVARIANT. `session_runner` is ALWAYS passed to the adapter. Without it the adapter decides
and submits inside `_decide_for` — bypassing lifecycle, journal, idempotency, risk and budget
entirely. There is no code path here that builds the strategy without one, and a test asserts that
by reading this source.

OFF BY DEFAULT via `strategies.QC27_ENABLED` (CLAUDE.md: all new automation gates default False).
Note that enabling IS trading for this strategy: an absent lifecycle row means TRADING (Operator,
2026-08-19), the same rule QC345 follows. That is deliberate — a strategy exists only because
somebody wrote it, wired it and shipped a deploy — but it means the settings flag is the last gate,
not the first of two.
"""

from __future__ import annotations

import logging
import re

from strategies import slot_reader as _settings_cache

_log = logging.getLogger(__name__)

#: Cockpit owns the tag, not the adapter. 001-004 are MANUAL, MOMENTUM, QC345 and BCTROT; the QC27
#: adapter has NO default tag and raises without one, precisely so this allocation is explicit.
#: A duplicate does not degrade — Nautilus raises at `Trader.add_strategy` and the node does not boot.
STRATEGY_ID = "TECHIVOL-005"
ORDER_ID_TAG = "005"

#: The allocation used when settings cannot be read AT BUILD TIME, and nowhere else. It is NOT this
#: lane's budget: `_allocated_equity()` is, read fresh every session.
#:
#: THIS WAS A CONSTANT AND THAT WAS THE BUG. `ALLOCATED_EQUITY = 20_000.0` was passed straight into
#: `RiskLimits` and never re-read, so `strategies.TECHIVOL-005` was a DEAD KNOB — an operator could
#: set it to 10000 and the lane would keep sizing 20000 until somebody shipped a deploy. It went
#: unnoticed for the lane's entire life because the constant AGREED with the setting: both were
#: 20000, so every observation was consistent with either. A test asserted `== 20_000.0` and passed,
#: measuring the wrong fact.
#:
#: Sizing off account equity is how a 20k strategy on a 100k account builds two oversized positions
#: where the research measured ten — the budget gate then refuses, so it reads as "buys the same and
#: gets rejected". That is why the fallback is this number and not `broker.equity()`.
_BUILD_TIME_FALLBACK_EQUITY = 20_000.0


def _allocated_equity() -> float | None:
    """THIS lane's configured allocation, or None when it genuinely cannot be read.

    THREE STATES, AND THE COLLAPSE OF TWO OF THEM IS THE DEFECT THIS EXISTS TO PREVENT:

      present  a number -> size off it
      zero     0.0      -> the operator wound this lane down; it reaches the runner AS 0.0 and the
                          lane declines to enter. `float(x) if x is not None` and never `x or 0.0`,
                          because 0.0 is falsy and `or` fuses zero into absent. On kumo-staging every
                          lane but BCTROT-004 is allocated 0 against a ~999k account, so that fusion
                          is ~1M of sizing for a lane somebody switched off.
      absent   None     -> no allocation configured. The runner falls back to the ACCOUNT, which is
                          its documented behaviour and what every running config passes.

    NONE IS NOT A REFUSAL, and cockpit must not pretend it is. kumo-trading-strategies confirmed (be244d9,
    0b5a591) that its side cannot tell "cockpit manages this lane but could not resolve it" from
    "no allocation configured" — both size off the account. So an unreadable settings store is
    logged HERE, loudly, because this is the last place that knows the difference.

    Never raises. A settings hiccup must not change how a live session sizes, and it must certainly
    not take the lane's EXITS down with its entries — a lane that cannot size should still be able
    to sell.
    """
    try:
        from api.settings import resolve

        target = resolve("strategies").get(STRATEGY_ID)
    except Exception as exc:  # noqa: BLE001
        _log.warning("%s allocation unreadable (%r) — sizing off the account, as before. This is the "
                     "one place that can tell 'unresolvable' from 'unconfigured'; downstream cannot.",
                     STRATEGY_ID, exc)
        return None
    return None if target is None else float(target)


def _limits_for_session(limits):
    """`limits` carrying this lane's CURRENT allocation. Never raises.

    Returns the limits UNCHANGED when the allocation is absent — leaving `allocated_equity` at
    whatever it already was rather than writing a guess over it.
    """
    from dataclasses import replace

    target = _allocated_equity()
    if target is None:
        return limits
    if "allocated_equity" not in getattr(type(limits), "__dataclass_fields__", {}):
        # THE FIELD MAY NOT EXIST. This repo pins kumo-trading-strategies by revision, and an unguarded
        # `replace()` raises TypeError mid-session — after the decision is journalled and before a
        # single order — which is the silent-death shape that has already cost this platform two
        # sessions. `getattr(..., {})` and not the direct attribute: the direct form raises
        # AttributeError on anything that is not a dataclass, making the guard the death it prevents.
        _log.warning("%s: installed kumo-trading-strategies RiskLimits has no `allocated_equity` — sizing "
                     "off the account, as before. Pin it to a revision that has the field.",
                     STRATEGY_ID)
        return limits
    return replace(limits, allocated_equity=target)



def _read_open_offset() -> int | None:
    """THIS lane's decision offset, re-read on every re-arm (#514). None keeps the current schedule.

    Returns the OFFSET only. The adapter derives the slot NAME from it (`open+{offset}m`), which is
    what keeps the name and the fire time from drifting apart — the ba37ef9 defect, where the journal
    said `open+5m` while the strategy filled at 12:00.
    """
    values = _settings_cache.resolve_cached("strategies")
    if values is None:
        return None
    raw = values.get(f"{STRATEGY_ID}_SLOTS") or []
    if not raw:
        return None                     # no override — the built-in offset stands
    return _offset_of(str(raw[0]))


def _invalidate_settings_cache() -> None:
    _settings_cache.invalidate()


def _offset_of(slot: str) -> int | None:
    """`"open+150m"` -> 150. None for any shape the adapter could not fire on."""
    m = re.fullmatch(r"open\+(\d+)m", slot.strip())
    return int(m.group(1)) if m else None


def _decision_slot() -> str:
    """`open+150m`, derived from the strategy's own shipped offset rather than spelled here.

    It is the idempotency key AND the string cockpit parses to schedule the alert, so a literal in
    this file is a second derivation of a fact kumo-trading-strategies already owns — and the two drifting
    is exactly the bug that made the slot read `open+5m` while the strategy filled at 12:00.
    """
    from kumo_strategies.runtime.executor.qc27_runner import DECISION_SLOT

    return DECISION_SLOT


def _slot_and_offset_from_settings() -> tuple[str, int]:
    """TECHIVOL-005's slot name and open-offset, from the SHARED derivation (see momentum.py).

    This module had the literal-vs-shipped-offset half right — `_decision_slot()` takes the default from
    kumo-trading-strategies rather than spelling it here — and the settings-vs-journal half still open: the
    offset came from settings while the NAME kept the default, so `TECHIVOL-005_SLOTS = ["open+45m"]`
    would schedule at 45m and journal every row under "open+150m".

    That is the third instance of one shape in three files, which is why the derivation moved out of
    each of them rather than being corrected in each of them.
    """
    from strategies.momentum import slot_and_offset_from_settings

    return slot_and_offset_from_settings(STRATEGY_ID, _decision_slot())


def _enabled() -> bool:
    """From SETTINGS, not the environment — whether a strategy is registered is an OPERATOR
    decision, and an env var makes it a deployment. Single source, no env fallback: a flag readable
    from two places is two derivations, and the one that loses is always the one somebody edited."""
    from api import settings

    return bool(settings.resolve("strategies").get("QC27_ENABLED"))


async def _held_claims(sm, strategy_id: str) -> set[str]:
    """Symbols this strategy still has a position claim on, whatever its universe says now.

    SUBSCRIBE TO WHAT WE HOLD, NOT ONLY TO WHAT WE MIGHT BUY. `NautilusBroker.submit()` refuses a
    symbol that is not a subscribed instrument, so a held name that has left the universe can be
    decided as an exit and refused every single session. Measured on TECHIVOL-005, 2026-08-21:

        SELL 11 XLV:   XLV is not a subscribed instrument
        SELL 26 WPM:   WPM is not a subscribed instrument
        SELL 56 WHD:   WHD is not a subscribed instrument
        SELL 174 CGAU: CGAU is not a subscribed instrument

    Eight orders, eight errors, one session — and the position stranded until something else cleared
    it. `build_momentum_strategy` was fixed for exactly this; the fix never reached the cross-sectional
    lanes, whose universes are OPERATOR-EDITABLE SETTINGS, so a name leaving while held is one edit
    away rather than an exotic case.
        A FAILURE HERE MUST NOT STOP THE STRATEGY BEING BUILT. This runs during synchronous node
    startup; raising takes the whole strategy out of the node, and a strategy that does not register
    is far worse than one that cannot sell a delisted holding -- `kernel.py`'s bare return already
    means a build failure leaves the node RUNNING with fewer strategies and no obvious cause. So an
    unreadable claims table degrades to the PREVIOUS behaviour (universe only) and says so loudly,
    rather than turning a sell problem into an inert lane.
"""
    from kumo_strategies.runtime.executor.store import PositionState, select

    try:
        async with sm() as s:
            rows = (await s.execute(select(PositionState).where(
                PositionState.strategy_id == strategy_id))).scalars().all()
    except Exception as exc:                                            # noqa: BLE001
        _log.warning(
            "%s: held claims unreadable (%r) — subscribing the universe ONLY. A position whose "
            "symbol has left the universe cannot be sold until this read works",
            strategy_id, exc)
        return set()
    return {r.symbol for r in rows}


def _universe_symbols() -> list[str]:
    """The tech universe to subscribe, from `strategies.QC27_UNIVERSE`.

    QC27's adapter takes a PRE-FILTERED instrument list: `filter_tech_universe` runs against a
    research `sectors.parquet` that does not exist in production, so sector membership cannot be
    derived live and must be supplied. The strategy still applies its OWN top-100 liquidity filter
    and top-10 momentum rank inside `select_portfolio`, so this list is the candidate pool, not the
    book.
    """
    from api import settings
    from api.lane_universes import universe_symbols

    # ONE reader with the IB connector's `load_contracts` (#871): the names ranked here are the
    # names IB is asked to resolve, or the lane boots "N of M symbols have no instrument".
    return universe_symbols(settings.resolve("strategies"), "QC27_UNIVERSE")


def _live_config():
    """The promoted config from `kumo_strategies`, with operator overrides from the settings domain.

    NOT the dataclass defaults, and not values spelled out here. `live_config()` carries the three
    measured departures — `momentum_price_field="close"` (a Nautilus bar cannot supply `close_adj`),
    `rebalance_period="D"` and the midday offset — each with the measurement that justifies it
    attached. Duplicating them in cockpit is how a runtime value drifts from the verified backtest.
    """
    from dataclasses import replace

    from kumo_strategies.strategies.qc27_tech_inverse_vol import live_config

    from api import settings

    cfg = live_config()
    domain = settings.resolve("strategies")
    overrides = {}
    for field in ("lookback_sessions", "realized_vol_window", "liquidity_filter_size",
                  "portfolio_size", "stop_loss_portfolio_frac", "cash_proxy_symbol",
                  "rebalance_period"):
        key = f"QC27_{field.upper()}"
        if domain.get(key) not in (None, ""):
            overrides[field] = domain[key]
    if not overrides:
        return cfg
    _log.info("%s: settings override %s", STRATEGY_ID, sorted(overrides))
    # `replace` on the frozen dataclass, so an override that names a field QC27 does not have raises
    # here at build time rather than being silently ignored.
    return replace(cfg, **overrides)


def build_qc27_strategy(*, feed=None):
    """TECHIVOL-005 wired to `QC27SessionRunner`, or None when the gate is off.

    RAISES when the gate is ON but the wiring is incomplete, rather than returning None — a strategy
    that is switched on and silently absent is the worst outcome available. `build_optional_strategy`
    in `engine_node` separates that from transient transport failure by exception TYPE.
    """
    if not _enabled():
        return None

    import asyncio

    from kumo_strategies.runtime.calendar import build_calendar
    from kumo_strategies.runtime.executor.pgjournal import PgJournal
    from kumo_strategies.runtime.executor.qc27_runner import QC27SessionRunner
    from kumo_strategies.runtime.executor.runner import RiskLimits
    from kumo_strategies.runtime.executor.store import create_all, make_engine, make_sessionmaker
    from kumo_strategies.runtime.nautilus.broker import NautilusBroker
    from kumo_strategies.runtime.nautilus.qc27_rotation import QC27RotationStrategy


    # UNIVERSE FIRST, before anything opens a database engine. An operator who set the gate and
    # forgot the universe should get that sentence, not a connection error from a store the
    # strategy never needed.
    symbols = _universe_symbols()
    if not symbols:
        raise RuntimeError(
            "strategies.QC27_ENABLED is on but `strategies.QC27_UNIVERSE` is empty — refusing to "
            "register a strategy that can never decide. QC27 ranks CROSS-SECTIONALLY over a tech "
            "pool it cannot derive live (sector membership comes from a research snapshot), so an "
            "empty universe is not a quiet no-op. Set the universe in settings, or unset the gate.")

    async def _prepare():
        # cross_loop: this runs inside a short-lived asyncio.run() during synchronous node startup,
        # while every later query runs on the TradingNode's loop. A pooled AsyncEngine would hand
        # out connections bound to a loop that no longer exists.
        eng = make_engine(None, cross_loop=True)
        await create_all(eng)
        sm = make_sessionmaker(eng)
        # SUBSCRIBE WHAT WE HOLD, IN THIS SAME `asyncio.run`. Universe ∪ held claims — see
        # `_held_claims` for the incident. A SECOND `asyncio.run` here fails with "no running event
        # loop": the engine is built `cross_loop=True` for the node's loop, not for a second
        # throwaway one, and `test_the_builder_CONSTRUCTS_a_real_strategy_end_to_end` caught it.
        # Held-but-unlisted names are added so they can be SOLD; they are not thereby buyable,
        # because ranking runs over the universe, not over the subscription.
        return sm, await _held_claims(sm, STRATEGY_ID)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        sm, held = asyncio.run(_prepare())
        symbols = sorted(set(symbols) | held)
    else:  # pragma: no cover — build runs before the node's loop exists
        raise RuntimeError(
            "build_qc27_strategy must run before the node's event loop starts — it opens a "
            "cross-loop engine for the lifecycle and journal stores")

    async def _read_state():
        """A FRESH lifecycle read per session. The runner holds a value object captured at
        construction, which live would freeze for the process lifetime: a HALT written on Tuesday
        would not be seen on Wednesday.

        AN ABSENT ROW IS TRADING (2026-08-19), the same rule `QC345SessionGateway._lifecycle`
        applies, and deliberately not re-derived: strategies do not appear in the database by
        accident, so a row was a second gate on an act that was already deliberate. An explicit row
        always wins. Accepted with it: a brand-new strategy trades on its first session.
        """
        from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State
        from kumo_strategies.runtime.executor.store import StrategyState, select

        async with sm() as s:
            row = (await s.execute(select(StrategyState).where(
                StrategyState.strategy_id == STRATEGY_ID))).scalars().first()
        return (Lifecycle(State(row.state), row.reason) if row
                else Lifecycle(State.TRADING, "default: no lifecycle row")).state

    async def _budget_allows(symbol: str, notional: float):
        """Cockpit's OWN `may_submit`, imported rather than reimplemented. Not a second budget rule:
        two derivations of one limit disagree, and the disagreement looks like a strategy quietly
        holding more than it was granted. Its value here is the EARLY, ATTRIBUTED refusal — "over
        budget" lands in the session result where an operator reads it, instead of as a rejection
        from the exec client after the order has already been built."""
        try:
            from api.budget_gate import may_submit
            from api.budget_store import load_book

            async with sm() as s:
                book = await load_book(s)          # takes a DB SESSION; calling it bare raises
            # SLEEVE, not BOOK, and keyword-only after the first (2026-08-21). This passed the whole
            # book positionally plus three arguments the signature does not accept:
            #
            #     may_submit(sleeve, *, is_entry, notional, currently_deployed)
            #
            # so every call raised `TypeError: may_submit() takes 1 positional argument but 4 were
            # given`, was caught below, and downgraded to "deferring to the exec client". TECHIVOL-005
            # logged it ten times on its first live session — once per intended entry — and the lane
            # has therefore never had a working budget pre-check. It blocked nothing, because the gate
            # is enforced again at submit; what was lost is the ATTRIBUTED refusal ("over budget", in
            # the session result an operator reads) in favour of a warning nobody reads.
            #
            # `currently_deployed` is derived from the broker the same way qc345.py:490 does it, so
            # the two lanes cannot disagree about what "deployed" means.
            sleeve = book.sleeves.get(STRATEGY_ID)
            entries = broker.position_entries()
            positions = broker.strategy_positions()
            deployed = sum(abs(positions.get(sym, 0)) * px for sym, px in entries.items())
            decision = may_submit(sleeve, is_entry=True, notional=notional,
                                  currently_deployed=deployed)
            return bool(decision.allowed), decision.reason
        except Exception as exc:                                        # noqa: BLE001
            # Deferring to the exec client, which enforces the same predicate at submission. A
            # pre-check that cannot run must not become a reason to stop trading.
            _log.warning("%s budget pre-check unavailable (%r) — deferring to the exec client",
                         STRATEGY_ID, exc)
            return True, "pre-check unavailable"

    cfg = _live_config()
    from strategies.momentum import _lane_symbols

    symbols = _lane_symbols(symbols)
    broker = NautilusBroker(strategy=None, instrument_ids=None)   # resolved by the strategy (#622)
    # ONE read for both halves. `offset_minutes` schedules the alert; the NAME has nowhere to go yet —
    # `QC27SessionRunner` takes no slot, in `__init__` or `run()`, so the journal cannot be told which
    # slot this was. That half is upstream's and is asserted explicitly in test_slot_offset_pair.py so
    # it is not mistaken for closed.
    _slot, offset_minutes = _slot_and_offset_from_settings()
    class _AllocationAwareRunner(QC27SessionRunner):
        """`QC27SessionRunner` that re-reads its allocation IMMEDIATELY BEFORE EVERY SESSION.

        WHY A SUBCLASS AND NOT A VALUE. `limits` is captured when the runner is constructed, which
        happens ONCE at node boot, and the node runs for days. `qc27_runner.run()` reads
        `self.limits.allocated_equity` at sizing time, so refreshing the attribute before delegating
        is enough — and it is the same reasoning that makes `read_state` a callable rather than the
        `lifecycle` value object beside it: an operator's edit on Wednesday must not be invisible
        because the process started on Monday.

        `qc345.py` does this in its own gateway's `_limits_for_session`. QC27's session loop lives
        upstream, so the refresh has to attach here instead — but it is the same fact, resolved from
        the same settings domain, and NOT a second derivation of it.
        """

        async def run(self, *args, **kwargs):
            # SETTINGS FIRST, THEN THE PLATFORM (#537). `_limits_for_session` refreshes from the
            # settings TARGET; `session_budget.resolve` then distributes parked capital and replaces
            # the basis with what the LEDGER says this lane actually holds — `min(actual, target)`,
            # the same rule `budget_gate.may_submit` bounds orders by.
            #
            # Sizing used to take the settings number and the gate the ledger's: two sources for one
            # fact, and QC345-003 was deployed 15,860 against a 10,000 sleeve.
            #
            # The settings read stays as the FALLBACK — an unreadable ledger keeps the previous
            # behaviour rather than inventing a new one.
            from dataclasses import replace

            from strategies.session_budget import resolve

            self.limits = _limits_for_session(self.limits)
            # THE SESSION IS THE IDEMPOTENCY KEY — see `session_budget.resolve`. Taken from the
            # call rather than invented, so a retry within one session lands once and the next
            # session distributes again.
            _sess = kwargs.get("session") or (args[1] if len(args) > 1 else None)
            budget = await resolve(
                STRATEGY_ID, fallback=getattr(self.limits, "allocated_equity", None),
                session=str(_sess) if _sess else None)
            if budget is not None and hasattr(self.limits, "allocated_equity"):
                self.limits = replace(self.limits, allocated_equity=budget)
            return await super().run(*args, **kwargs)

    runner = _AllocationAwareRunner(
        journal=PgJournal(sm, strategy_id=STRATEGY_ID),
        # `lifecycle` is the constructed fallback and is deliberately the SAFE one: if `read_state`
        # were ever dropped, this must not read as "trades by default".
        lifecycle=_disabled_lifecycle(),
        cfg=cfg,
        broker=broker,
        # THE LANE'S OWN CAPITAL, not the account's — and re-read every session by the subclass
        # above, so this value only decides the FIRST session after a boot. See `_allocated_equity`.
        limits=RiskLimits(allocated_equity=_allocated_equity() or _BUILD_TIME_FALLBACK_EQUITY),
        strategy_id=STRATEGY_ID,
        read_state=_read_state,
        budget_gate=_budget_allows,
    )
    strategy = QC27RotationStrategy(
        cfg=cfg,
        symbols=symbols,
        order_id_tag=ORDER_ID_TAG,
        # require_exchange: this path can place orders, so the holiday-unaware weekday fallback is
        # refused rather than silently accepted — it would trade on a market holiday.
        # The venue's OWN sessions when its adapter supplies them (#628), else Alpaca as before.
        # `require_exchange=True` is UNCHANGED and still refuses the holiday-unaware fallback — an
        # injected calendar SATISFIES that requirement rather than relaxing it.
        calendar=build_calendar(
            require_exchange=True,
            calendar=getattr(feed, "_venue_calendar", None),
        ),
        history_days=220,          # 100-session warmup plus the 63-session lookback, in CALENDAR days
        # The ADAPTER's own copy, used only by `_decide_for`'s sizing — the path taken when no
        # `session_runner` is passed. Cockpit ALWAYS passes one (this module's one invariant), so
        # this number is never read in production. It is resolved rather than hardcoded anyway: a
        # fallback that silently disagrees with the real allocation is how a bypass path stops
        # looking like a bypass.
        equity=_allocated_equity() or _BUILD_TIME_FALLBACK_EQUITY,
        # LIVE SLOT RE-READ (#514), passed only if the INSTALLED adapter accepts it — this repo
        # pins kumo-trading-strategies by revision and the seam arrived in db25413. See `slot_reader`.
        **_settings_cache._live_reread_kwargs(
            QC27RotationStrategy, read_open_offset=_read_open_offset),
        session_runner=runner,     # <- the whole point of this module
        open_offset_minutes=offset_minutes,
        # CLAIMS NOTHING, deliberately. `external_order_claims` are EXCLUSIVE node-wide and raise
        # InvalidConfiguration at `Trader.add_strategy`; QC27's tech pool overlaps MOMENTUM's and
        # BCTROT's by construction, so any claim stops the node booting. QC27 holds nothing yet.
        external_order_claims=None,
    )
    from api.venue_preference import prefer_primary_exchange

    # WHICH IDENTITY TO KEEP when IB reports a symbol on two venues (#625). 64 of staging's 209
    # cached instruments carry two, because `exchange="SMART"` makes IB return contract details for
    # multiple listings. Without this the resolver keeps whichever the cache iterated LAST, and a
    # lane asks for `SPY.XNAS` — an instrument that never has data — and starves in silence.
    #
    # A LAMBDA OVER THE STRATEGY, not over a cache captured now: `self.cache` is empty at build and
    # populated by the time `on_start` resolves. Capturing it here would preserve the emptiness,
    # which is the trap #622 already paid for once.
    strategy._prefer_venue = lambda sym, cands, _s=strategy: prefer_primary_exchange(_s.cache, sym, cands)
    broker.strategy = strategy
    # FEED, so `exit()` can release the shares. `NautilusBroker.exit()` is the ONLY path that cancels a
    # resting protective stop before selling — `submit()` releases nothing — and it refuses outright when
    # `self.feed is None`. This was assigned only in momentum.py, so QC345 and TECHIVOL sold straight into
    # a book where every share was reserved: measured at the venue, all six held symbols had
    # `qty_available = 0`, and `available: 0` means UNRESERVED is 0, not that the position is gone. Every
    # sell got `403 insufficient qty available`, structurally, not conditionally. (#459; the routing half
    # is kumo-trading-strategies 53e2ff5, which now sends SELLs through `exit()` rather than `submit()`.)
    broker.feed = feed
    return strategy


def _disabled_lifecycle():
    from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State

    return Lifecycle(State.DISABLED, "fallback: read_state not wired")
