"""MOMENTUM-001 — the systematic rotation, hosted by the engine node.

Until now this strategy ran as its own process: an asyncio loop woke it once a day and it sent
orders straight to Alpaca's REST API. That put every order outside the node — no RiskEngine
pre-trade check, nothing in the cache, nothing for reconciliation to anchor on, and a cockpit UI
that could not see a position the strategy was holding. CLAUDE.md puts execution in Nautilus; this
is what that actually means in practice.

Here the node owns everything it should: the clock (`Strategy.clock.set_time_alert`), the market
data (the Alpaca client already feeds `dailyBars`), and order submission. Postgres keeps what
Nautilus has no opinion about — the symbol pool, the operator lifecycle, the action log, and the
durable give-back trail.

    OFF BY DEFAULT. `KUMO_MOMENTUM_ENABLED` must be set explicitly, per the CLAUDE.md rule that all
    new automation gates default False. Enabling it is NOT the same as letting it trade: the
    lifecycle state comes out of Postgres, and only an operator can move it to TRADING. A freshly
    enabled strategy computes and publishes; it submits nothing.
"""

from __future__ import annotations

from typing import NamedTuple

import asyncio
import logging
import os

from strategies import slot_reader as _settings_cache

_log = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("KUMO_MOMENTUM_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


class SessionGateway:
    """Runs one session, building a FRESH runner each time.

    The runner is not constructed once and reused, because the operator can arm, pause or halt the
    strategy from the UI while the node keeps running. A runner captured at startup would hold the
    lifecycle it was born with and happily submit orders for a strategy that has since been paused.
    Re-reading state per session is also what makes the give-back trail correct — it lives in
    Postgres precisely because the old per-request runner reseeded it from memory every time.
    """

    def __init__(self, sm, pool, journal, jobs, cfg, broker, limits, strategy_id="MOMENTUM-002",
                 tradable_symbols=None, on_result=None):
        self._sm, self._pool, self._journal, self._jobs = sm, pool, journal, jobs
        self._cfg, self._broker, self._limits = cfg, broker, limits
        self._strategy_id = strategy_id
        # Seam for anything that needs to observe a finished session without the RUNNER knowing it
        # exists (#199 A). The runner lives in kumo-trading-strategies and must not import a cockpit module;
        # this gateway is cockpit-owned, so the dependency points the right way. Called best-effort:
        # an observer that raises must not fail a trading session.
        self._on_result = on_result
        # What this node actually subscribed to at startup. The pool moves underneath it, so the
        # runner needs to know which pool names it can genuinely reach.
        self._tradable = tradable_symbols


    async def record_terminal(self, session: str, symbol: str, ok: bool, detail: str) -> None:
        """The venue's ACTUAL answer, called from the adapter's order-event handlers (#383).

        SIGNATURE IS FIXED BY THE CALLER, POSITIONALLY. `momentum_rotation._record_terminal` does
        `getattr(self._runner, "record_terminal", None)` and then `record(session, sym, ok, detail)`.
        Any other arity or order fails inside a Nautilus event handler — the worst place to find out —
        and the same shape on both gateways means one contract and one test.

        WHY THIS WAS MISSING, AND WHAT IT COST. `_record_terminal` returns early when the runner has no
        such attribute, so for weeks it no-opped on every fill and nothing said so. Measured on
        2026-08-20: 22 OrderFilled events, ZERO terminal rows journalled, on BOTH strategies. The #51
        loop has never closed in production. `grep -c record_terminal` on this file returned 0.

        A submit-time `phase="result"` row records that NAUTILUS accepted the order. This is the row
        that arrives later, when the venue has actually answered — a fill, or a rejection/denial/
        cancellation that submit-time acceptance could not predict.

        Best-effort BY DESIGN: called from a live event handler outside any session's control flow, so a
        failure here must never propagate into Nautilus's dispatch. It closes the audit loop; it is not
        load-bearing the way intent-before-submit is.

        The row mirrors `PgSessionRunner.record_terminal` exactly — `phase`/`ok` are the keys the retry
        counter reads, and two writers of one fact that disagree is worse than one that is absent.
        """
        try:
            await self._journal.write(
                "order" if ok else "error",
                f"terminal: {symbol} {'filled' if ok else 'rejected'} — {detail}",
                session=session, symbol=symbol, detail={"phase": "terminal", "ok": ok},
            )
        except Exception as exc:  # noqa: BLE001 — never raise into a Nautilus event handler
            _log.warning("terminal row not journalled for %s/%s: %r", session, symbol, exc)

    async def sync_claim(self, symbol: str, qty: int, px: float | None) -> None:
        """The claim, brought back to what the lane's OWN cache position says (#829). NEVER RAISES.
        Called from the strategy's terminal order handlers — fill, rejection, denial, cancel — with
        `(symbol, the lane's NETTING quantity, fill price or None)`, POSITIONALLY: the same fixed
        contract as `record_terminal`, for the same reason.

        ONE PREDICATE, EVERY GATEWAY (`api.claims_sync.sync_claim_from_book`, #950). This body and
        QC345's were identical and both read a NEGATIVE netting quantity as flat (a sign-blind
        positive test), so a short lane's claim was DROPPED on every terminal event. Zero is flat and
        nothing else is.
        """
        from api.claims_sync import sync_claim_from_book
        await sync_claim_from_book(self._journal, self._strategy_id, symbol, qty, px)

    async def _lifecycle(self):
        from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State
        from kumo_strategies.runtime.executor.store import StrategyState, select

        async with self._sm() as s:
            row = (await s.execute(select(StrategyState).where(
                StrategyState.strategy_id == self._strategy_id))).scalars().first()
        # AN ABSENT ROW IS TRADING (2026-08-19). Inverted from "absent means DISABLED".
        #
        # The old rule treated an unenabled strategy as untrusted. the operator's objection is the correct one:
        # strategies do not appear randomly in the database. One exists only because somebody wrote it,
        # wired it into `engine_node.build_node`, and shipped a deploy — so the lifecycle row was a
        # second gate on an act that was already deliberate. Its real effect was that BCTROT-004 and
        # QC345-003 sat registered, RUNNING and submitting nothing for days until a row was hand-written
        # in psql, with no button and no endpoint to write it from.
        #
        # The restart worry the old comment names is still covered, by the ROW and not by this default:
        # anything ever moved to HALTED or DISABLED HAS a row, and an explicit row always wins here.
        #
        # Accepted, deliberately: a brand-new strategy trades on its first session, and an EMPTY
        # lifecycle table — fresh database, restore that lost rows — reads as "everything trades".
        return (Lifecycle(State(row.state), row.reason) if row
                else Lifecycle(State.TRADING, "default: no lifecycle row"))

    async def _save_if_unchanged(self, life, expected) -> bool:
        """Persist a state the RUNNER produced, but only over the state it was given.

        ONE WRITER, shared with every other gateway (`strategies/lifecycle_state.py`). The rationale
        for compare-and-set, and for why an absent row must INSERT rather than read as an operator
        override, lives there — this had both rules in one place and only the first one right.
        """
        from . import lifecycle_state

        return await lifecycle_state.save_if_unchanged(
            self._sm, self._strategy_id, life, expected)

    async def _current_state(self):
        return (await self._lifecycle()).state

    async def urgent_pending(self) -> bool:
        """Is there an operator action that should not wait for the next scheduled session?

        LIQUIDATING, or a blacklist on something we hold. Cheap enough to ask once a minute.
        """
        from kumo_strategies.runtime.executor.lifecycle import State

        life = await self._lifecycle()
        if life.state is State.LIQUIDATING:
            return True
        if not life.state.may_submit_exits:
            return False
        async with self._sm() as s:
            from kumo_strategies.runtime.executor.store import PositionState, select
            held = (await s.execute(select(PositionState.symbol).where(
                PositionState.strategy_id == self._strategy_id))).scalars().all()
        for sym in held:
            if await self._pool.must_liquidate(sym):
                return True
        return False

    def _limits_for_session(self):
        """`self._limits` with this strategy's CURRENT allocation, read fresh.

        Falls back to the constructed limits when settings are unreadable — sizing off the account is
        today's behaviour and a settings hiccup must not change how a live session sizes. It is logged,
        because silently reverting to the wrong number is how this defect survived unnoticed.
        """
        from dataclasses import replace

        try:
            from api.settings import resolve

            # `.get(...)` then an EXPLICIT None check, never `... or 0.0`. The `or` form fused two
            # different instructions into one value BEFORE anything downstream could tell them
            # apart: a MISSING key and a key that ARRIVED AS 0 both became 0.0, so this lane could
            # not express a zero at all. Third site of one defect — kumo-trading-strategies had the same
            # `or` at two (be244d9).
            raw = resolve("strategies").get(self._strategy_id)
            target = None if raw is None else float(raw)
        except Exception as exc:                                        # noqa: BLE001
            _log.warning("allocation unreadable for %s (%r) — sizing off the account, as before",
                         self._strategy_id, exc)
            return self._limits
        if target is None:
            # ABSENT, not zero: nobody configured this lane. Unchanged limits, which means the
            # account — this lane's long-standing documented behaviour, left alone deliberately
            # rather than quietly tightened in a change about something else.
            _log.info("%s has no configured allocation (key absent) — sizing off the account, as "
                      "before", self._strategy_id)
            return self._limits
        if target <= 0:
            # 0 is BOTH the schema default and the operator's way of saying "wind down". It used
            # to be inexpressible downstream — `pgrunner`'s `allocated_equity or broker.equity()`
            # treated 0.0 as falsy — which is why this returned unchanged limits and let the
            # whole-account fallback happen. kumo-trading-strategies be244d9 fixed that: 0.0 now MEANS zero
            # and the lane declines to enter. So a zero is passed through as a zero.
            #
            # WHY THIS IS SAFE, not merely bad (kumo-trading-strategies review): a strategy wound to target=0
            # has actual > target, so `is_reducing` is true, and `budget_gate.may_submit` refuses EVERY
            # entry before sizing is ever consulted. The whole-account fallback is therefore unreachable
            # in the wind-down case — a different mechanism catches it first. It IS reachable for an
            # UNCONFIGURED strategy holding nothing (target 0, actual 0, not reducing), which is why
            # this logs rather than passing silently.
            _log.info("%s is allocated ZERO (target=%s) — sizing to nothing, not to the account. "
                      "The lane declines to enter; its EXITS are unaffected.",
                      self._strategy_id, target)
            target = 0.0
        # THE FIELD MAY NOT EXIST. `RiskLimits` gained `allocated_equity` in kumo-trading-strategies; this repo
        # pins that dependency by URL with NO commit and NO minimum version, and the local venv and the
        # running container currently DISAGREE about whether the field is there. An unguarded
        # `replace()` raises TypeError before the runner is built and before any journal row — the exact
        # silent-death shape of the `slot` kwarg that killed two sessions and started this work. Found by
        # code review; refusing to reintroduce it one layer down.
        if "allocated_equity" not in type(self._limits).__dataclass_fields__:
            _log.warning(
                "%s: installed kumo-trading-strategies RiskLimits has no `allocated_equity` — sizing off the "
                "account, as before. Pin the dependency to a revision that has it.", self._strategy_id)
            return self._limits
        return replace(self._limits, allocated_equity=target)

    def _opens_kwarg(self, opens) -> dict:
        """`{"opens": ...}` if the INSTALLED runner accepts it, `{}` otherwise.

        NARROW ON PURPOSE. An earlier version built the WHOLE kwarg dict here, which moved
        `slot=slot` out of the runner call line and broke
        `test_slot_reaches_the_journal_or_the_idempotency_key_is_a_lie` — a guard that exists because
        a swallowed slot collides BCTROT-004's daily decisions on one idempotency row and loses the
        second. That test is a source assertion naming the call site deliberately, and hiding the
        call behind a helper defeats it. `jobs` and `slot` stay literal at the call; only the
        conditional part lives here.

        CONDITIONAL SO COCKPIT SHIPS FIRST. `opens` arrives from issue 118 and today's
        deployed runner has no such parameter, so forwarding it unconditionally would break this
        change the moment it ships and force a lockstep deploy. Guarded, cockpit is backward
        compatible: an older runner simply never sees it.

        AND IT SAYS SO WHEN IT DROPS ONE. `_live_reread_kwargs` drops unaccepted kwargs silently, and
        `test_installed_strategies_accept_what_we_pass`'s docstring records the cost: two lanes kept a
        slot captured at build, the suite ran against a revision production does not run, and the
        wiring test was green throughout. A dropped `opens` means the overnight-gap filter reads
        NOTHING while the journal still reports "gap filter declined N entries" — declared and inert.
        Three states: forwarded, nothing to forward, and could-not-forward, which is loud.
        """
        from inspect import signature

        from kumo_strategies.runtime.executor.pgrunner import PgSessionRunner

        if opens is None:
            return {}
        if "opens" in signature(PgSessionRunner.run).parameters:
            return {"opens": opens}
        _log.error(
            "%s: the installed PgSessionRunner.run does not accept `opens`, so the overnight-gap "
            "filter is INERT this session — entries are NOT being filtered on the gap, whatever the "
            "decision journal says. Align the kumo-trading-strategies pin (needs #118 or later).",
            self._strategy_id)
        return {}

    async def run(self, panel, session: str, jobs=None, slot: str = "open+5m", opens=None):
        """One session for one SLOT.

        `slot` is DEFAULTED and must stay that way. kumo-trading-strategies calls this from two places with
        two different shapes — `momentum_rotation._session_coro` passes `jobs=` and `slot=`, while
        `qc345_rotation` passes neither. Making either one required swaps which strategy dies.

        `opens` IS DEFAULTED FOR THE SAME REASON, and it is why this signature is a contract rather
        than a detail. issue 118 calls `self._runner.run(..., slot=slot, opens=opens)`;
        without the parameter here that is a TypeError at the decision row, killing BCTROT-004 AND
        MOMENTUM-002 — MOMENTUM too, because the keyword is passed whether or not that lane's gap
        filter is configured. Exactly the August `slot=` incident, which also died before any order.

        NEITHER REPO'S TESTS COVERED THIS DIRECTION. `test_installed_strategies_accept_what_we_pass`
        asserts the outbound half; kumo-trading-strategies' runner-protocol test binds the runners in THAT
        repo, and this gateway is not one of them. `test_gateway_accepts_what_strategies_call.py` is
        the inbound half and exists because of this.

        It is threaded through to the runner and to the journal rather than merely accepted:
        `uq_exec_one_decision_per_session` is keyed `(strategy_id, session, slot)`, so a swallowed
        slot would make BCTROT-004's two daily decisions collide on one row and lose the second.
        """
        from kumo_strategies.runtime.executor.broker import ReadOnlyBroker
        from kumo_strategies.runtime.executor.pgrunner import PgSessionRunner

        life = await self._lifecycle()
        entered_with = life.state
        # ALLOCATION IS RE-READ HERE, NOT CAPTURED AT CONSTRUCTION (#334, build-spec item 2).
        #
        # `RiskLimits()` was built with no `allocated_equity`, so `pgrunner`'s sizing line
        # (`equity = self.limits.allocated_equity or self.broker.equity()`) has ALWAYS taken the second
        # branch: a 20k sleeve buying ~10k a name off a ~100k account, which is what overflowed
        # MOMENTUM-002 to 67k and turned `is_reducing` on.
        #
        # Read per session for the same reason the RUNNER is rebuilt per session: an operator edits the
        # target from the UI while the node keeps running. QC345's target was cut 40k -> 20k on
        # 2026-08-15 mid-flight. A value captured at construction is a stale second copy of a fact
        # settings owns — the Law 1 defect this fix exists to remove, not to reintroduce one layer down.
        limits = self._limits_for_session()
        # Defence in depth. The runner already refuses to submit unless the state permits it; giving
        # it a broker that cannot reach money means a bug in that check costs nothing.
        #
        # ReadOnlyBroker, not DryRunBroker: reads must stay REAL. A dry-run broker reports an empty
        # account, which would make SHADOW compute entries for names already held and no exits at
        # all — publishing decisions that are fiction, when those published decisions are the entire
        # basis for deciding whether to arm it. It would also make the session-start reconcile
        # release every position claim, since nothing would appear backed by a broker position.
        broker = self._broker if life.state.may_submit_exits else ReadOnlyBroker(self._broker)
        result = await PgSessionRunner(
            pool=self._pool, journal=self._journal, lifecycle=life, cfg=self._cfg,
            broker=broker, limits=limits, strategy_id=self._strategy_id,
            tradable_symbols=self._tradable,
            # Last look before money moves. The state above was read at session start and a session
            # can run for many seconds; an operator who halts in that window must not watch it
            # submit anyway.
            recheck_state=lambda: self._current_state(),
        ).run(panel, session, jobs=jobs if jobs is not None else self._jobs, slot=slot,
              **self._opens_kwarg(opens))
        # Only write back a state the RUNNER changed -- normally a risk-breach HALT -- and only if
        # the operator has not moved it underneath us in the meantime.
        if life.state != entered_with:
            # ONE OUTCOME, ONE STORY. This used to journal "the operator changed the state" for every
            # falsy answer, which was true for one of three cases and named an innocent human in the
            # other two — including the one where the state was simply never persisted (#459).
            from . import lifecycle_state

            outcome = await self._save_if_unchanged(life, entered_with)
            if outcome == lifecycle_state.OPERATOR_WON:
                await self._journal.write(
                    "risk",
                    f"runner wanted {life.state.value} ({life.reason}) but the operator changed the "
                    f"state during the session — theirs stands",
                    session=session, slot=slot)
            elif outcome == lifecycle_state.CONTRADICTION:
                await self._journal.write(
                    "risk",
                    f"runner wanted {life.state.value} ({life.reason}) and it was NOT PERSISTED — the "
                    f"stored state and the state this session began at ({entered_with.value}) "
                    f"disagree. NOBODY CHANGED ANYTHING; this lane will read as its default next "
                    f"session, so treat it as unhalted until the row is fixed by hand",
                    session=session, slot=slot)
        if self._on_result is not None:
            try:
                await self._on_result(result)
            except Exception as exc:                                    # noqa: BLE001
                # Observers are strictly downstream of trading. A broken notifier costs an alert,
                # never a session.
                _log.warning("session observer failed (ignored): %r", exc)
        return result


# `_universe` LIVED HERE AND IS DELETED (#652 item 7): it was the TTL cache for the Alpaca-backed
# `_instrument_ids` that #622 removed, and nothing read it since — a module var whose only remaining
# consumer was a test monkeypatch measures the test, not the code. `_lane_symbols` was also defined
# twice with identical bodies (the later shadowing the earlier); the single definition lives below.


def _lane_symbols(symbols: list[str]) -> list[str]:
    """The symbols this lane will trade — the single place a lane's pool is finalised (#622).

    It exists because the concept does. `_instrument_ids` used to be where every lane's pool passed
    through on its way to becoming instrument ids, and deleting it left three lanes each computing
    the same list inline with nothing naming it. `tradable_symbols` on the gateway is derived from
    exactly this list.

    It does NOT resolve venues, and that is the whole point of #622: a venue is a runtime fact only a
    connected adapter can produce, so it is resolved in the strategy's `on_start` instead. This is
    the last point at which the pool is still just names.

    RESTORED after PR #683 and PR #685 each deleted "the duplicate": the module briefly held two
    identical copies, both PRs were right that one had to go, and git auto-merged the two deletions
    into zero — three call sites referencing nothing, caught by the exactly-once pin.
    """
    return list(symbols)


#: Nautilus's own id for a fill nobody claimed — a real `StrategyId`, not a sentinel of ours. Phantom
#: legs sit under it, so it appears in the open book beside real lanes while being the opposite of
#: one: the record that attribution FAILED, never a party to a collision.
_UNATTRIBUTED = "EXTERNAL"


def _resolve_claims(claimed, cache, strategy_id: str, _sink=None) -> list:
    """Claimed SYMBOLS -> `InstrumentId`s, at BUILD (#622 follow-up, #197 B8).

    NOT LAZY, and the asymmetry with `symbols` is the whole point. Measured in the installed
    nautilus_trader, `system/kernel.py`:

        1024  await _await_engines_connected()
        1028  await _await_execution_reconciliation()    <- RECONCILIATION
        1039  _trader.start()                            <- on_start

    `symbols` can resolve at `on_start` because SUBSCRIPTION happens after connect. Claims cannot,
    because RECONCILIATION happens BEFORE `on_start` — and claims exist precisely to catch
    reconciliation-generated flatting orders. Registering a claimant afterwards protects against an
    event that has already been booked, while looking like it worked.

    With no claimant, Nautilus books that flatting order under EXTERNAL and NETTING's
    `{instrument}-{strategy}` id makes it a PHANTOM position instead of closing the real one. HSBC
    sat as a -93 short the broker had never heard of.

    THE COLD-START GAP IS REAL AND IS REPORTED, NOT HIDDEN. This reads the durable cache, which a
    genuinely cold node does not have. Survivable where the same dependency was not for symbols —
    every boot needs symbols, only a node with existing positions needs claims — but staging had 22
    non-flat positions against a fresh container, so "usually empty" is not "always empty". A claim
    that could not resolve is NAMED, and resolving NONE is said out loud: "0 of 3" must not read the
    same as a lane that deliberately claims nothing.
    """
    say = _sink or (lambda m: _log.error("%s", m))
    wanted = list(claimed or [])
    if not wanted:
        return []
    by_symbol = {i.symbol.value: i for i in cache.instrument_ids()}

    # A CLAIM ON A SYMBOL SEVERAL LANES HOLD IS A GUESS, NOT A CLAIM (#749).
    #
    # `get_external_order_claim` returns THE claiming strategy, so whoever registered becomes the
    # destination for every reconciliation correction on that instrument — regardless of whose shares
    # actually moved. Measured on a live instance book: 31 of 59 symbols are held by more than one lane, and
    # BCTROT deliberately claims nothing (claims are node-exclusive and both lanes share one pool).
    # So every correction caused by BCTROT's shares landed on MOMENTUM. Not a bad guess — a guess
    # presented as a fact, and the mechanism behind ~$94,000 of fabricated proceeds sitting on
    # MOMENTUM and EXTERNAL.
    #
    # REFUSING SENDS THOSE TO EXTERNAL INSTEAD, which is visibly unattributed: it breaks the per-lane
    # sum in a way an operator can chase, rather than silently crediting a lane that did not trade.
    # Unambiguous symbols keep their claimant and keep HSBC's protection (#197 B8) — narrowing must
    # not become switching off.
    #
    # NON-ZERO HOLDINGS ONLY. The cache retains CLOSED positions, so nearly every symbol shows a
    # sibling lane at quantity zero; counting those would refuse almost the whole book and
    # reintroduce the phantom this exists to prevent.
    holders: dict[str, set[str]] = {}
    for pos in (cache.positions_open() or []):
        try:
            qty = float(getattr(pos, "quantity", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0 and hasattr(pos, "quantity"):
            continue
        # EXTERNAL IS NOT A LANE, and counting it as one turns this narrowing into a RATCHET. A
        # symbol held by one real lane plus one open phantom leg would look like two holders, so the
        # claim would be refused; refusing removes the protection, so the next reconciliation
        # flatting order books under EXTERNAL as a NEW phantom, which keeps the symbol ambiguous on
        # every subsequent boot. The bucket for unattributed money would decide — permanently — that
        # the already damaged symbols stay unprotected, which is precisely backwards.
        holder = str(pos.strategy_id)
        if holder == _UNATTRIBUTED:
            continue
        sym = getattr(getattr(pos.instrument_id, "symbol", None), "value", None)
        if sym:
            holders.setdefault(sym, set()).add(holder)
    shared = [s for s in wanted if len(holders.get(s, set())) > 1]
    if shared:
        say(f"{strategy_id}: REFUSING to claim {', '.join(shared)} — more than one lane holds each, "
            f"so a claim would make this lane the destination for corrections caused by another "
            f"lane's shares. Their reconciliation orders will book under EXTERNAL, which is "
            f"unattributed rather than mis-attributed (#749).")
    wanted = [s for s in wanted if s not in set(shared)]

    ids = [by_symbol[s] for s in wanted if s in by_symbol]
    missing = [s for s in wanted if s not in by_symbol]
    if missing:
        say(f"{strategy_id}: {len(ids)} of {len(wanted)} claimed symbols resolved; NOT claimed: "
            f"{', '.join(missing)}. An unclaimed instrument lets a reconciliation flatting order "
            f"book under EXTERNAL as a phantom position (#197 B8).")
    return ids


# A second, identical `_lane_symbols` LIVED HERE AND IS DELETED (#640). 8dddc43 (#632) re-added
# the definition 2eafb69 (#622) already had, ~55 lines up — an edit that anchored blindly. Python
# bound this later copy, so the one above was dead while reading as "the single place". One copy,
# enforced by `api/test_no_shadowed_definitions.py`.


# `_instrument_ids` LIVED HERE AND IS DELETED (#622).
#
# It built `TICKER.MIC` ids from ALPACA's asset list — `EXCHANGE_TO_MIC` plus a live GET of
# /v2/assets — on every venue, for every lane. So an IBKR-only instance could not construct a single
# strategy without an Alpaca API key: `build_node()` raised, 13 restarts, `/positions` served [] with
# 22 non-flat positions held at the broker.
#
# It was not carelessness. `MomentumRotationStrategy(instrument_ids=...)` demanded *symbol + venue* at
# CONSTRUCTION, and the venue half is a runtime fact only a connected adapter can produce — nothing
# else can satisfy that signature offline. The fix was to change the obligation, not the source: the
# lanes hand over SYMBOLS and the strategy resolves at `on_start`, where Nautilus has already loaded
# the attached adapter's instruments (kernel.py:1024 awaits connect before :1039 starts the trader).
#
# A symbol is what this layer knows. The pool is a research artifact; it has no opinion about XNAS vs
# XNYS and must not be forced to have one.
#: MOVED these to settings would silently shrink the live portfolio the moment a domain was absent.
_RESEARCHED = {"n_hold": 8, "buffer": 5, "give_back_frac": 0.5}


#: The lowest take-profit target an operator may set, in ATR multiples (#1055). The lab's grid never
#: went below 3.25 and < 3 ATR measured ~-9pp; the settings schema's `minimum` for
#: `<PREFIX>_TAKE_PROFIT_ATR` is asserted equal to this so the two cannot drift.
TAKE_PROFIT_ATR_FLOOR = 3.0


class LiveOverrides(NamedTuple):
    """What `_live_overrides` returns: the three researched knobs, and the take-profit target that is
    OFF unless written (#1055). A NamedTuple so a caller reads `.take_profit_atr` and the next knob
    does not move every positional unpack (b4enxqbg on #1055)."""

    n_hold: int
    buffer: int
    give_back_frac: float
    take_profit_atr: float | None


def _live_overrides(prefix: str = "MOMENTUM") -> LiveOverrides:
    """`(n_hold, buffer, give_back_frac, take_profit_atr)` — researched values unless an operator has
    overridden them; `take_profit_atr` has NO researched value and is None (off) unless written (#1055).

    Reads `<PREFIX>_N_HOLD`, `<PREFIX>_BUFFER`, `<PREFIX>_GIVE_BACK_FRAC`, `<PREFIX>_TAKE_PROFIT_ATR`
    from the `strategies` domain. Every key read here MUST be declared in `strategies.schema.json`:
    `additionalProperties: false` strips an undeclared key before `resolve` returns, so the function
    would read the researched value forever while the file reads as applied (#1054 — measured on both
    tenants; the guard is `test_momentum_overrides_survive_the_schema.py`).

    THE PREFIX IS THE WHOLE POINT (#794). This read `MOMENTUM_*` unconditionally, and `bctrot_config()`
    is `replace(live_config(), ...)` — so all three keys reached BCTROT-004 as well, live, on the next
    config re-read, with nothing saying so. That was harmless while the lanes deliberately shared one
    config to keep their comparison at one variable. Since 2026-09-04 they differ by schedule, ranking
    and an entry filter, so a knob named for one lane silently steering the other is a control whose
    name lies about its blast radius — an operator narrowing MOMENTUM's book during an incident would
    narrow BCTROT's too.

    DEFAULTED TO "MOMENTUM", so every existing caller and every seeded `MOMENTUM_*` value behaves
    exactly as before; only BCTROT passes something else.
    Absent, empty or unreadable means the researched value, so an instance with no settings behaves
    EXACTLY as today — which is what makes this landable before anything is seeded.

    `settings.resolve` never raises and fills schema defaults, so the failure mode here is not an
    exception but a WRONG DEFAULT — hence the researched values live in this module and the domain is
    consulted only for an explicit override.
    """
    n_hold, buffer_, give_back = (
        _RESEARCHED["n_hold"], _RESEARCHED["buffer"], _RESEARCHED["give_back_frac"])
    # OFF unless written. `ExitConfig.take_profit_atr` defaults to None and `needs_atr` is False, so
    # an instance that never writes the key trades exactly as before this key existed.
    take_profit: float | None = None
    try:
        from api import settings

        domain = settings.resolve("strategies")
    except Exception:  # noqa: BLE001 — an unreadable domain must not change how a live lane sizes
        return LiveOverrides(n_hold, buffer_, give_back, take_profit)
    for key, cast, name in ((f"{prefix}_N_HOLD", int, "n_hold"),
                            (f"{prefix}_BUFFER", int, "buffer"),
                            (f"{prefix}_GIVE_BACK_FRAC", float, "give_back_frac"),
                            (f"{prefix}_TAKE_PROFIT_ATR", float, "take_profit_atr")):
        raw = domain.get(key)
        if raw in (None, ""):
            continue
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            continue  # a malformed override is not a reason to size differently
        if name == "n_hold":
            n_hold = value
        elif name == "buffer":
            buffer_ = value
        elif name == "give_back_frac":
            give_back = value
        else:
            # THE FLOOR IS GUARDED ONCE, HERE, and the schema's `minimum` is asserted EQUAL to it by
            # `test_take_profit_atr_reaches_the_runner.py` — two derivations of one bound, pinned.
            # Below the floor is OFF, never a smaller target: the lab's grid never went below 3.25
            # and < 3 ATR costs ~9pp; 0 would fire on the first tick above entry.
            take_profit = value if value >= TAKE_PROFIT_ATR_FLOOR else None
    return LiveOverrides(n_hold, buffer_, give_back, take_profit)


def live_config(overrides_prefix: str = "MOMENTUM"):
    """THE configuration this cockpit deploys — importable on its own, so it can be asserted on.

    Extracted from `build_momentum_strategy`, which needs Postgres and a resolved symbol pool and so
    cannot be called from a test. While the config was built inside it, the only way to gate it was a
    hardcoded copy of these values living in kumo-trading-strategies — which proves nothing about what ships,
    because the copy and the original drift the moment anyone edits here. `test_live_config.py`
    asserts against this function.

    exclude_instrument_types is set EMPTY on purpose, and that is a real decision rather than a
    default. The gate needs a symbol->type map to do anything, live has no classifier for one, and
    passing instrument_type={} against a config that still listed exclusions meant the config claimed
    to exclude ETFs and ADRs while trading them -- declared and inert, the same shape as the venue
    default and the enum comparison. The config now says what actually happens.

    It matters for this pool: BNO EUFN EMGF VFLO WT IYZ are ETFs and HSBC BCS LYG MUFG SMFG RY TD
    SAN UBS are ADRs, so roughly a quarter of the names are affected. ledger-provider holds them deliberately,
    so trading them is defensible -- silently trading them while the config says otherwise is not.
    Supply a classifier to turn the gate back on. (Tracked as B3 on kumo-trading-platform issue 197: the validated
    research ran WITH these exclusions, so live is trading a wider universe than the one measured.)

    OPERATOR OVERRIDES, from the `strategies` settings domain (#486). The researched values below stay
    the DEFAULT — they are not moving to settings, they are gaining a way to be changed. That
    distinction is the whole design: schema defaults are `n_hold=5, buffer=3`, so a version that
    *moved* these would silently shrink the live portfolio from 8 names to 5 on the two lanes holding
    the most capital, and nothing would report it.

    Why it is worth having at all: changing `n_hold` today means editing source, rebuilding an image
    and restarting a live engine — the riskiest operation this system performs, and the one that
    crash-looped it on 2026-08-19. QC345 and QC27 already read their promoted config from settings;
    this was the gap.

    BCTROT-004 SHARES THIS CONFIG, deliberately — "same pool, same engine, same exits as MOMENTUM-002,
    so the live comparison is ONE change rather than two". An override therefore moves BOTH lanes.
    Giving them separate knobs is a real decision about the experiment, not a refactor, and it would
    need a signature change here to express.
    """
    from kumo_strategies.strategies.momentum_rotation.config import (
        ExitConfig,
        GateConfig,
        MomentumRotationConfig,
        PortfolioConfig,
    )

    n_hold, buffer_, give_back, take_profit = _live_overrides(overrides_prefix)

    return MomentumRotationConfig(
        # n_hold=8 / buffer=5 IS the researched configuration, not an unvalidated operator override.
        # An earlier comment here claimed the opposite — that every research run used the
        # PortfolioConfig defaults of 5/3 — and that was wrong: the verified backtest that produced
        # the live evidence sets 8/5 explicitly (kumo-trading-strategies
        # research/residual-gate/run_verified.py:45), alongside give_back_frac=0.5 below. Corrected
        # 2026-08-09; the stale version sent an incident investigation down the wrong path twice.
        # (The separate finding that concentration helps — "3 beat 5 beat 10" — still stands as a
        # reason to revisit 8, but it is a question to test, not a description of what shipped.)
        portfolio=PortfolioConfig(n_hold=n_hold, buffer=buffer_),
        # Every rule set here must be one the LIVE runner implements — the others are silently
        # ignored by the thing holding real positions. Enforced by test_live_config.py, and for the
        # operator keys by `test_take_profit_atr_reaches_the_runner.py` (every declared
        # `<PREFIX>_<RULE>` key is in ks `LIVE_SUPPORTED_EXITS`).
        # `take_profit_atr` is None unless the operator wrote the key (#1055, ks#221 step 2): the
        # runtime computes the ATR only when `needs_atr` says a rule wants it, so absent = off.
        exits=ExitConfig(give_back_frac=give_back, take_profit_atr=take_profit),
        gates=GateConfig(exclude_instrument_types=()),
    )


#: A decision slot is "open+5m" / "open+150m" / "close-20m" — an offset from a US session boundary.
_SLOT_RE = __import__("re").compile(r"^(open|close)[+-][0-9]{1,3}m$")


from strategies.decision_slots import BUILTIN_SLOTS  # one declaration of the built-ins (#378)


def decision_slots_from_settings(strategy_id: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """The strategy's decision times, from SETTINGS, falling back to its built-in schedule.

    WHY THIS EXISTS. The operator asked repeatedly for a way to change a strategy's trigger time by hand. There
    was none: the slots were Python constants, so moving a decision by ten minutes meant editing source,
    rebuilding an image and restarting a live engine — the riskiest operation this system performs, and
    the one that crash-looped it on 2026-08-19. Every other operational knob in this repo is already
    settings-driven; the schedule was the gap.

    NOT the generated `qc345` domain (`api/settings/generated.py`). That walks kumo-trading-strategies' config
    DATACLASSES, and `execution.decision_slots` is not in the group it walks — its 17 keys are all
    ranking parameters. Rather than change what upstream generates, this rides the constructor argument,
    which already wins over the config: `momentum_rotation.py:196` takes `decision_slots` verbatim when
    passed and only falls back to `cfg.execution` when it is not.

    FAIL SAFE, IN BOTH DIRECTIONS. Unreadable settings, an empty list, or a malformed slot all return the
    built-in schedule rather than a partially-applied one. A typo must not silently move a decision, and
    it must not stop the strategy from ever deciding either — a strategy that never runs is the worst
    outcome, because nothing announces it.
    """
    try:
        from api.settings import resolve

        raw = resolve("strategies").get(f"{strategy_id}_SLOTS") or []
    except Exception as exc:  # noqa: BLE001
        _log.warning("%s: decision slots unreadable (%s) — using the built-in %s", strategy_id, exc, default)
        return default
    return resolve_slots(raw, default, strategy_id=strategy_id)


def resolve_slots(raw, default: tuple[str, ...], *, strategy_id: str = "") -> tuple[str, ...]:
    """The PURE half: a raw settings value + a built-in -> the schedule the lane will run.

    Extracted so the never-ran detector resolves through the SAME rule instead of reading the raw
    value itself (#378): an empty override means "keep the built-in" to the lane and used to mean
    "nothing is due" to the detector, which is why it could not fire on any lane. Two derivations
    of one fact; there is one now, and it is this function.
    """
    if not raw:
        # SAY SO. This returned the built-in SILENTLY, so "no override is configured" and "my
        # settings write never reached this process" produced identical output — nothing.
        #
        # That cost three consecutive failed attempts to arm a lane on 2026-09-01, each looking like
        # success: `make up` re-seeds settings and discarded an API PUT; then a PUT immediately
        # before `docker restart` had not propagated; then the API reported the new value while the
        # engine still read the old one. In every case the engine logged nothing about slots at all.
        #
        # Three states, never two: OVERRIDDEN, NO OVERRIDE CONFIGURED, UNUSABLE.
        _log.info(
            "%s: no decision-slot override configured — using the built-in %s. If you just changed "
            "this in settings, the engine has NOT seen it: a `make up` re-seeds settings from the "
            "instance, and a write must reach the settings volume before the process restarts.",
            strategy_id, list(default),
        )
        return tuple(default)
    slots = tuple(str(x).strip() for x in raw if str(x).strip())
    bad = [x for x in slots if not _SLOT_RE.match(x)]
    if bad or not slots:
        _log.warning(
            "%s: decision slots %s are not usable (bad: %s) — using the built-in %s. A slot looks like "
            "open+5m, open+150m or close-20m.", strategy_id, list(raw), bad, default,
        )
        return tuple(default)
    if slots != tuple(default):
        _log.warning("%s: decision slots OVERRIDDEN by settings: %s (built-in %s)", strategy_id, list(slots), default)
    return slots


def slot_and_offset_from_settings(strategy_id: str, default_slot: str) -> tuple[str, int]:
    """A strategy's decision slot NAME and the minutes after the open it fires at — from ONE read.

    RETURNED AS A PAIR, and shared rather than copied, because the same defect has now appeared three
    times in three files. Each instance read the configured slot, kept the half it needed and silently
    dropped the other:

        QC345  kept the minutes, journalled the literal — decided at 12:00 under a row named "open+5m"
        QC27   the same code copied hours later, with only half the drift fixed
        and both modules' own docstrings described the bug they still had

    The arithmetic was never wrong. What was wrong is that a module doing its own parse is free to keep
    one half, so the name and the time can be edited apart. A tuple cannot.

    THE SLOT STRING IS NOT A LABEL. It is part of
    `uq_exec_one_decision_per_session (strategy_id, session, slot)` and it is what
    `decided_this_session` and `_resume` match on. A wrong name means the record claims a decision at a
    time that never happened, and a session the strategy never ran can be refused as already decided —
    which presents as a silent skip.

    Offsets from the OPEN only, because the strategies that use this schedule a single alert as
    `open_offset_minutes`. A `close-20m` slot has nowhere to go, so BOTH halves fall back together — a
    fallback replacing only the offset would create the divergence it exists to avoid.
    """
    slots = decision_slots_from_settings(strategy_id, (default_slot,))
    chosen = slots[0] if len(slots) == 1 and slots[0].startswith("open+") else None
    if chosen is None:
        _log.warning(
            "%s: decision slots %s cannot be honoured — it schedules ONE alert as an offset from the "
            "open, so exactly one `open+Nm` slot is required. Using the built-in %s.",
            strategy_id, list(slots), default_slot,
        )
        chosen = default_slot if default_slot.startswith("open+") else "open+5m"
    return chosen, int(chosen.removeprefix("open+").removesuffix("m"))



def _read_slots_for(strategy_id: str, default: tuple[str, ...]):
    """This lane's decision slots, re-read on every re-arm (#514). None keeps the current schedule.

    Returns NAMES, not an offset — the momentum family schedules by slot name and supports several per
    session (BCTROT decides at midday AND near the close), which an offset could not express.
    """
    values = _settings_cache.resolve_cached("strategies")
    if values is None:
        return None
    try:
        return decision_slots_from_settings(strategy_id, default)
    except Exception as exc:  # noqa: BLE001 — a lane that stops arming looks like one that held
        _log.warning("%s: slots unreadable on re-arm (%r) — keeping the current schedule",
                     strategy_id, exc)
        return None


def build_momentum_strategy(feed=None):
    """MOMENTUM-002, or None when the gate is off. Unchanged behaviour."""
    from kumo_strategies.runtime.nautilus.momentum_rotation import MomentumRotationStrategy

    return _build_rotation(
        MomentumRotationStrategy, feed=feed, strategy_id="MOMENTUM-002",
        identity={"decision_slots": decision_slots_from_settings("MOMENTUM-002", BUILTIN_SLOTS["MOMENTUM-002"]),
                  # LIVE SLOT RE-READ (#514). Without it the schedule is captured here and the
                  # settings knob is dead until a redeploy. See `slot_reader` for why it must be
                  # sync, cheap and non-raising: `_arm` runs on the Nautilus clock callback.
                  "read_slots": lambda: _read_slots_for("MOMENTUM-002", BUILTIN_SLOTS["MOMENTUM-002"])},
                           claims_from="MOMENTUM-002")


def build_bctrot_strategy(feed=None):
    """BCTROT-004 — the same rotation decided at midday and near the close (issue 32).

    2026-08-16: register it and transition half of MOMENTUM's budget to it; both run alongside
    and BCTROT replaces MOMENTUM over time with no cutoff.

    Same pool, same engine, same exits (`give_back_frac=0.5`) as MOMENTUM-002 — deliberately, so the
    live comparison is ONE change rather than two. Only identity and schedule differ.

    CLAIMS NONE, and that is a decision rather than an omission. `external_order_claims` are
    EXCLUSIVE node-wide and collide at `Trader.add_strategy`, which raises InvalidConfiguration —
    BCTROT and MOMENTUM trade the same pool, so any instrument given to both stops the node booting.
    Claims exist so a strategy can adopt the synthetic flatting order reconciliation generates for
    its OWN position, and during the wind-down every such position is MOMENTUM's. BCTROT has nothing
    to adopt until it holds something.

    TAG 004 IS PASSED EXPLICITLY. The adapter DEFAULTS to 003, which QC345 holds — cockpit allocates,
    and passing it is what keeps the node bootable.

    REGISTERING IT LETS IT TRADE. An absent lifecycle row reads as TRADING (2026-08-19) --
    this said the opposite, promising an operator gate between the deploy and the first order.
    """
    from kumo_strategies.runtime.nautilus.bctrot_rotation import BCTRotationStrategy

    return _build_rotation(
        BCTRotationStrategy, feed=feed, strategy_id="BCTROT-004",
        identity={"strategy_name": "BCTROT", "order_id_tag": "004",
                  "decision_slots": decision_slots_from_settings(
                      "BCTROT-004", BUILTIN_SLOTS["BCTROT-004"]),
                  "read_slots": lambda: _read_slots_for(          # live re-read (#514)
                      "BCTROT-004", BUILTIN_SLOTS["BCTROT-004"])},
        claims_from=None,
        config_factory=bctrot_config,
    )


def bctrot_config():
    """BCTROT-004's config. MOMENTUM-002 keeps `live_config()` EXACTLY as it was.

    This function is the signature change `live_config`'s docstring said would be needed: the two
    lanes shared one config so that the live comparison was "one change, one number", and splitting
    them is a real decision about the experiment rather than a refactor. The operator asked for it on
    2026-09-04, so BCTROT now differs from MOMENTUM by schedule AND ranking AND an entry filter.
    THE ONE-CHANGE COMPARISON IS OVER — read the two lanes' divergence from here on as a bundle, not
    as evidence about the schedule.

    Two changes, both from `research/residual-gate/FINDINGS-gap-entry.md`:

    `lookback=40` over the live 20. The only parameter in that programme confirmed OUT OF SAMPLE:
    corrected t 2.67 vs 0.79 at the 10-day horizon in sample, block bootstrap IC +0.130 with a 95%
    CI of [+0.010, +0.214], and then the same winning cell on 140 sessions (2025-03-27..2025-10-15)
    that no parameter in the programme had ever seen. Its max-|t| null across the search grid is
    p=0.100 — marginal on the in-sample panel alone, which is exactly why the out-of-sample window
    is what justifies shipping it.

    `min_abs_gap_pct=0.015` skips entries whose overnight gap sits inside +/-1.5%: the flat middle
    is 45% of candidates and where the return disappears. Smooth threshold curve, and it holds
    across n_hold 5/8/10, give-back 0.35, lookback 40 and exits off entirely.

    NO SLOT LIST. The gap is `today's open / yesterday's close`, fixed once the market opens, so all
    three of BCTROT's decisions reach the same verdict with nothing remembered between them. An
    earlier version read the FILL price and needed a slot list to hide the fact that by midday it
    was measuring the day's move so far instead.

    NOT INCLUDED, deliberately: `max_hold_days=15`, which the same file recommends alongside these.
    It is one of the exit rules `PgSessionRunner` does not implement, so configuring it would trip
    the entry-suppression guard and drift the book to cash. `unsupported_live_exits` catches it, and
    `test_live_config.py` is the gate — but it is worth naming here so the omission reads as a
    decision rather than an oversight.

    NOT INCLUDED, for a different reason: `max_correlation`. Its sweep is a non-monotone zigzag
    (14.12 / 15.10 / 12.78 / 17.21 / 11.40 / -2.20 as the cap tightens) and the same file's verdict
    is "no cap value is supported". `n_hold` behaves the same way, so 8 stays.
    """
    from dataclasses import replace

    # BUILT ON `live_config("BCTROT")`, not on the bare call (#794). `_live_overrides` is read
    # INSIDE `live_config`, so a `replace()` on top of the default cannot undo an operator's
    # `MOMENTUM_N_HOLD` — by then it is already baked in. The prefix is the only place the two lanes
    # can be separated, and it has to be passed at the point the overrides are READ.
    base = live_config("BCTROT")
    return replace(
        base,
        score=replace(base.score, lookback=40),
        # `size_to_book` IS DELIBERATELY NOT SET, and the reason is worth more than the +2.72pp
        # it measured. It divides the gross by the book AT ENTRY and nothing rebalances afterwards,
        # so three names entered while the book was thin take 80% of equity permanently and the
        # weights end up decided by ARRIVAL ORDER — when five more qualify the next session there
        # is cash for two of them.
        #
        # The correct fix is target-based sizing: compute what each position SHOULD be, compare
        # with what it IS, and trade the difference. That is implemented and measured in the
        # backtest (`PortfolioConfig.rebalance_band`, +1.24pp at band 25% with sharpe and drawdown
        # equal or better at every band) but it cannot go live yet: a negative delta is a PARTIAL
        # SELL, and `NautilusBroker.exit` cancels the position's resting protective stop before
        # selling. Sell part of a position that way and the remainder is left with its stop
        # cancelled and nothing re-arming it. Re-arming protection on a remainder is cockpit-side
        # work that does not exist.
        #
        # Its measured benefit is also a thin-book artefact: the book held a mean of 3.40 names
        # against n_hold=8 for the whole panel, which is exactly the regime where concentrating
        # helps and where the arrival-order defect never surfaces. Both numbers come from the same
        # property of the data.
        #
        # So BCTROT ships the three changes that were measured on their own terms, and the sizing
        # question stays open behind issue 115.
        execution=replace(base.execution, min_abs_gap_pct=0.015),
    )


def _build_rotation(strategy_cls, *, strategy_id: str, identity: dict, claims_from: str | None,
                    config_factory=None, feed=None):
    """Shared assembly for the rotation family. Returns the strategy or None when the gate is off.

    Raises when the gate is ON but the wiring is incomplete. A strategy that is switched on and
    silently absent is the worst outcome available: the operator sees the flag set, the node comes
    up clean, and nothing ever trades or says why.
    """
    if not _enabled():
        return None

    from kumo_strategies.runtime.calendar import build_calendar
    from kumo_strategies.runtime.executor.pgjobs import PgJobRunner
    from kumo_strategies.runtime.executor.pgjournal import PgJournal
    from kumo_strategies.runtime.executor.pgpool import PgSymbolPool
    from kumo_strategies.runtime.executor.runner import RiskLimits
    from kumo_strategies.runtime.executor.store import create_all, make_engine, make_sessionmaker
    from kumo_strategies.runtime.nautilus.broker import NautilusBroker
    from kumo_strategies.strategies.momentum_rotation.candidates import StaticList

    async def _held_claims(sm):
        """Symbols this strategy still has a position claim on, whatever the pool says now."""
        from kumo_strategies.runtime.executor.store import PositionState, select

        if claims_from is None:
            return set()          # claims are per strategy; a strategy holding nothing claims nothing
        async with sm() as s:
            rows = (await s.execute(select(PositionState).where(
                PositionState.strategy_id == claims_from))).scalars().all()
        return {r.symbol for r in rows}

    async def _prepare():
        # cross_loop: this runs inside a short-lived asyncio.run() during synchronous node startup,
        # and every later query runs on the TradingNode's loop. A pooled AsyncEngine would hand out
        # asyncpg connections bound to a loop that no longer exists, and the first real query would
        # fail at the first session -- before any durable row exists to explain why.
        eng = make_engine(None, cross_loop=True)
        await create_all(eng)
        sm = make_sessionmaker(eng)
        # IDENTITY IS EXPLICIT. `PgJournal` defaults to MOMENTUM-002, and this function builds every
        # rotation strategy — so an anonymous journal wrote BCTROT-004's entire durable record under
        # MOMENTUM-002's name (issue 54). A default that is right for exactly one caller is
        # silently wrong for every other.
        pool, journal = PgSymbolPool(sm), PgJournal(sm, strategy_id=strategy_id)
        # SUBSCRIBE TO WHAT WE HOLD, NOT JUST WHAT WE MIGHT BUY. The pool is the buy list and it
        # changes: ledger-provider closes a name and it leaves. If we still hold it, subscribing only to the
        # pool means no instrument id, so `NautilusBroker.submit()` refuses it as "not a subscribed
        # instrument" and the strategy cannot sell its own position -- the exit is decided every
        # session and refused every session.
        held = await _held_claims(sm)
        return (sm, pool, journal, PgJobRunner(sm, pool, journal),
                sorted(set(await pool.symbols()) | held), sorted(held))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        sm, pool, journal, jobs, symbols, claimed = asyncio.run(_prepare())
    else:  # pragma: no cover - build_node is called before the node's loop exists
        raise RuntimeError(
            "build_momentum_strategy must run before the node's event loop starts — it reads the "
            "symbol pool synchronously to know what to subscribe to")

    if not symbols:
        raise RuntimeError(
            "KUMO_MOMENTUM_ENABLED is set but the symbol pool is empty — nothing to subscribe to. "
            "Check the pool sources before enabling the strategy.")

    symbols = _lane_symbols(symbols)
    # PER LANE. Defaulted to `live_config` so MOMENTUM-002 and every future caller are
    # unchanged by BCTROT needing its own; passing the factory rather than the config keeps
    # the operator-override re-read inside it (`_live_overrides` runs per build).
    cfg = (config_factory or live_config)()

    # The broker needs the strategy and the strategy needs the gateway that holds the broker, so
    # the reference is closed after construction rather than threaded through three constructors.
    # The BROKER asks the strategy for resolved ids now (#622) — it used to keep its own copy
    # built by the same call, which is two derivations of one fact.
    broker = NautilusBroker(strategy=None, instrument_ids=None)
    # Operator alerts on a finished session (#199 A). Engine-side, because the API process cannot
    # see a session result — health and drift alerts live over there for the mirror-image reason.
    from strategies.session_alerts import build_session_observer

    # FULL CAPITAL BY INTENT (#806). 1.0 means "deploy the whole sleeve", not "we expect to
    # land exactly 100%". Reality lands lower — spread and cash shortfall skip entries — and
    # that shortfall is a MEASUREMENT, never a number to bake in here. A fractional cap like
    # 0.97 encodes a guess about the venue as if it were policy, and hides the real constraint.
    # The strategy must size so it does not over-deploy; `budget_allows` DENIES anything that
    # would (api/providers/gated_exec.py), so an over-reach is refused before the venue, not
    # silently truncated.
    gateway = SessionGateway(sm, pool, journal, jobs, cfg, broker, RiskLimits(max_deployed_frac=1.0),
                             strategy_id=strategy_id,
                             # The DECLARED pool. It used to be the RESOLVED ids, which are not known
                             # until on_start; the broker refuses anything that did not resolve.
                             tradable_symbols=set(symbols),
                             # The lane's OWN identity (#651 item 1). Built once per lane; with the
                             # old MOMENTUM-002 default, BCTROT's degradation alert wore MOMENTUM's
                             # key and the two lanes suppressed and cleared each other's alarms.
                             on_result=build_session_observer(strategy_id=strategy_id))
    strategy = strategy_cls(
        cfg=cfg,
        # require_exchange: this path can place orders, so the holiday-unaware fallback is refused
        # rather than silently accepted. A live node with missing credentials must fail at startup,
        # not schedule a session on Thanksgiving.
        # The venue's OWN sessions when its adapter supplies them (#628), else Alpaca as before.
        # `require_exchange=True` is UNCHANGED and still refuses the holiday-unaware fallback — an
        # injected calendar SATISFIES that requirement rather than relaxing it.
        calendar=build_calendar(
            require_exchange=True,
            calendar=getattr(feed, "_venue_calendar", None),
        ),
        source=StaticList(symbols),      # the live pool is read per session from Postgres
        symbols=symbols,
        instrument_type={},
        history_days=180,                # live: request enough history to clear warmup
        # Minutes after the OPEN to decide. 5 is the intended value and what the research assumes;
        # it is overridable so a session can be forced later in the day when the normal alert has
        # already passed — the first live run needed that to exercise the order path at all rather
        # than wait a day to discover whether it works.
        open_offset_minutes=int(os.environ.get("KUMO_MOMENTUM_OPEN_OFFSET_MIN", "5")),
        session_runner=gateway,
        session_jobs=jobs,
        # `read_slots` is dropped here when the INSTALLED adapter predates db25413, so a
        # revision skew degrades to the captured slot rather than failing to build (#514).
        **_settings_cache._live_reread_kwargs(
            strategy_cls, **{k: v for k, v in identity.items() if k == "read_slots"}),
        **{k: v for k, v in identity.items() if k != "read_slots"},
        # Claim ONLY what this strategy currently holds a position claim on — not the whole pool.
        #
        # Fixes #197 B8: with no claimant, Nautilus books a reconciliation-generated flatting order
        # under EXTERNAL, and NETTING's `{instrument}-{strategy}` position id turns that into a
        # PHANTOM position instead of closing the real one. HSBC sat as a -93 short the broker had
        # never heard of until it was removed by hand.
        #
        # Scope matters, and narrow is not timidity here: claims are EXCLUSIVE node-wide and collide
        # at `Trader.add_strategy`, which raises InvalidConfiguration — so claiming the pool would
        # take the WHOLE NODE down the moment a pool name overlapped another strategy's holding.
        # FIG is exactly that case today: a momentum candidate that MANUAL-001 actually holds.
        # `_held_claims` reads MOMENTUM-002's own position rows, so it cannot contain someone
        # else's symbol.
        #
        # The residual gap: a position opened DURING a session is unclaimed until the next restart,
        # because StrategyConfig is frozen. It is the startup-held set that reconciliation flattens
        # after a restart, which is the case that actually bit us.
        # RESOLVED AT BUILD, deliberately — see `_resolve_claims`. Reconciliation runs before
        # on_start, so a claim registered later protects against an event already booked.
        external_order_claims=(
            _resolve_claims(claimed, feed.cache, strategy_id)
            if (claimed and claims_from and feed is not None) else None
        ),
    )
    # WHICH IDENTITY TO KEEP when IB reports a symbol on two venues (#625). 64 of staging's 209
    # cached instruments carry two, because `exchange="SMART"` makes IB return contract details for
    # multiple listings. Without this the resolver keeps whichever the cache iterated LAST, and a
    # lane asks for `SPY.XNAS` — an instrument that never has data — and starves in silence.
    #
    # A LAMBDA OVER THE STRATEGY, not over a cache captured now: `self.cache` is empty at build and
    # populated by the time `on_start` resolves. Capturing it here would preserve the emptiness,
    # which is the trap #622 already paid for once.
    from api.venue_preference import prefer_primary_exchange

    strategy._prefer_venue = lambda sym, cands, _s=strategy: prefer_primary_exchange(_s.cache, sym, cands)
    broker.strategy = strategy
    # THE EXIT PATH NEEDS THE FEED STRATEGY, AND `strategy` ABOVE IS NOT IT (#358).
    #
    # `release_for_exit` — which cancels the protective stop reserving the shares so an exit can fill —
    # lives on UiFeedStrategy, because every helper it needs does: `_cancel_reducing_leg`,
    # `_await_reducing_orders_clear`, `_await_shares_available`, and the protection reconciler whose
    # standoff it sets. `broker.strategy` is the ROTATION strategy (MomentumRotationStrategy), a
    # different object with no delegation between them.
    #
    # This was shipped once with `NautilusBroker.exit()` calling `self.strategy.release_for_exit(...)`,
    # which raises AttributeError. `pgrunner._submit` does not catch it, so it would have aborted the
    # WHOLE session — exits AND entries — after the first symbol's intent row was already journalled,
    # turning a partial rotation failure into a total one. Both sides' tests were green: one patched the
    # method onto a double, the other bound it to a host. Neither used the object the broker holds.
    broker.feed = feed
    return strategy
