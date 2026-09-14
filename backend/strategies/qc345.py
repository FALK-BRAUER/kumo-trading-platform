"""QC345-003 — monthly top-down rotation, hosted safely by the engine node (#324).

WHY THIS FILE EXISTS AT ALL, AND WHY IT IS NOT `SessionGateway`
---------------------------------------------------------------
`QC345RotationStrategy` submits orders ITSELF when no `session_runner` is passed. `_try_decide`
branches on exactly that:

    if self._runner is not None:  self._run_session(...)      # hand it to cockpit
    else:                         self._decide_for(...)       # submit directly, right here

So `node.trader.add_strategy(QC345RotationStrategy(...))` with the default argument is not "QC345
registered but idle". It is a strategy that will place live orders on the next monthly rebalance
with NO lifecycle read, NO journal, NO idempotency key, NO risk check and NO budget gate. Every
control this repo built lives on the cockpit side of that branch.

Cockpit already owns a session gateway — `strategies/momentum.SessionGateway` — but it cannot be
reused here. It builds a `PgSessionRunner`, and that runner is momentum-specific in the ways that
matter: it takes a `MomentumRotationConfig`, resolves candidates from the Postgres symbol pool, and
decides with the momentum engine. QC345 takes a `QC345RotationConfig`, resolves its universe by
COMPUTING it (`QC345ComputedSource`), and decides with `qc345_rotation.decide`. Handing QC345's
panel to the momentum runner would not fail loudly; it would rank the wrong universe with the wrong
config and look like a working session.

What IS shared is the lifecycle contract, and that is deliberately duplicated in behaviour but not
in code path: absent row = DISABLED, SHADOW computes and publishes, only TRADING may submit
entries, LIQUIDATING may still exit. A test pins that both gateways read the same store and honour
the same `State` flags, because two implementations of one rule drift.

    OFF BY DEFAULT, via `strategies.QC345_ENABLED` in settings (CLAUDE.md: all new automation gates
    default False). The gate is the whole gate: ONCE ENABLED, QC345 TRADES.

    This paragraph used to read [absent-row: historical] "with no lifecycle row QC345 is DISABLED, so it computes, journals
    and publishes while submitting nothing until an operator moves it to TRADING", describing a
    second safety gate that does not exist. `_lifecycle` (below) returns TRADING for an absent row
    and has since 2026-08-19. Prose that reads as a safety property is the most dangerous kind of
    wrong, because it survives review by sounding careful — a staging runbook stated this same rule
    exactly inverted and was believed by two readers.
"""

from __future__ import annotations

import inspect

import logging
import os
from types import SimpleNamespace

from strategies import slot_reader as _settings_cache

_log = logging.getLogger(__name__)

#: Cockpit owns this, not the adapter. The adapter DEFAULTS to tag 003 and cockpit happens to agree,
#: but it is passed explicitly all the same — a default that silently matches is not an allocation,
#: and the 003 -> 004 -> 003 churn on BCTROT came from reading a tag off someone else's code.
STRATEGY_ID = "QC345-003"
ORDER_ID_TAG = "003"

#: QC345 rebalances monthly at the open plus five minutes. One decision per session, so one slot.
DECISION_SLOT = "open+5m"


#: Minutes after the open at which QC345 decides, when settings say nothing. Kept as the parsed form of
#: `DECISION_SLOT` so the two cannot drift.
_DEFAULT_OPEN_OFFSET_MIN = int(DECISION_SLOT.removeprefix("open+").removesuffix("m"))



def _read_open_offset() -> int | None:
    """QC345's decision offset, re-read on every re-arm (#514). None keeps the current schedule.

    Same contract as qc27's: offset only, so the adapter keeps deriving the slot NAME from it and the
    two cannot drift apart (ba37ef9, where the journal said `open+5m` while the fill was at 12:00).
    """
    from strategies.qc27 import _offset_of

    values = _settings_cache.resolve_cached("strategies")
    if values is None:
        return None
    raw = values.get(f"{STRATEGY_ID}_SLOTS") or []
    if not raw:
        return None
    return _offset_of(str(raw[0]))


def _slot_and_offset_from_settings() -> tuple[str, int]:
    """QC345's slot name and open-offset, from the SHARED derivation (see momentum.py).

    Was a private parse here. It kept the minutes and dropped the name, so an operator moving the
    decision time left the journal naming a slot the strategy never used. QC27 then copied the same
    shape. One derivation now, for every strategy.
    """
    from strategies.momentum import slot_and_offset_from_settings

    return slot_and_offset_from_settings(STRATEGY_ID, DECISION_SLOT)


def _enabled() -> bool:
    """From SETTINGS, not from the environment.

    It was an env var, and the operator's objection is the right one: whether a strategy is registered is an
    OPERATOR decision, and an env var makes it a deployment. Changing it meant editing compose and
    restarting the stack — for a choice with a form field two clicks away.

    Environment is for what must be known BEFORE settings can be read: the database URL, Redis, the
    settings directory itself. This is not that. `build_qc345_strategy` already resolves the `qc345`
    domain for its config, so settings are demonstrably reachable at exactly this point.

    Single source. No env fallback, deliberately — a flag readable from two places is two
    derivations of one fact, and the one that loses is always the one somebody edited.
    """
    from api import settings

    return bool(settings.resolve("strategies").get("QC345_ENABLED"))



class QC345SessionGateway:
    """Runs one QC345 session under cockpit's lifecycle, journal, risk and budget controls.

    A FRESH lifecycle read per session, for the same reason `SessionGateway` does it: an operator can
    pause or halt from the UI while the node keeps running, and a state captured at startup would
    happily submit for a strategy that has since been stopped.

    The order of operations is not arbitrary — each step can only refuse, and the cheapest and most
    absolute refusals come first:

        1. lifecycle    DISABLED/WARMUP/HALTED do not even decide
        2. idempotency  a slot that already decided must not decide again
        3. decide       pure engine, no side effects
        4. journal      write the decision BEFORE any order — a crash between them must leave
                        evidence that we intended to trade, not silence
        5. budget       per-order, via the SAME predicate the exec client enforces
        6. submit       entries only in TRADING; exits also in LIQUIDATING
    """

    def __init__(self, sm, journal, cfg, broker, limits, *, strategy_id: str = STRATEGY_ID,
                 assets=None, assets_builder=None, slot: str = DECISION_SLOT, on_result=None, daily_loss_armed: bool = False):
        self._sm, self._journal, self._cfg = sm, journal, cfg
        self._broker, self._limits = broker, limits
        self._strategy_id = strategy_id
        # UNARMED BY DEFAULT (#784). `daily_loss_frac` resolves to 0.05 from a bare `RiskLimits()`,
        # the basis is the SHARED ACCOUNT rather than this lane's sleeve (#517), and QC345's decision
        # rows sit up to 8 days apart — so an armed 5% would mean "5% of the whole account since up
        # to a week ago", which an ordinary market week trips. Wiring the mechanism and choosing the
        # number are separate decisions; the number is the operator's.
        self._daily_loss_armed = bool(daily_loss_armed)
        # Symbol -> name/exchange(/status), for the fundamental-like filter inside the source.
        #
        # LAZY, FROM THE CACHE (#647/#624). `assets_builder` is a zero-arg callable evaluated at the
        # FIRST `_decide`, because that is when it can work: the frame is derived from the
        # instruments the attached adapter loaded (`Instrument.info`, via the venue-neutral
        # `asset_meta` accessor), which exist only after `on_start` resolved them — while THIS
        # object is constructed at build time, before the node's loop starts. The old shape fetched
        # Alpaca's /v2/assets eagerly at build, which meant a hardcoded paper URL and APCA
        # credentials in the lanes layer, and on staging-ibkr — which holds no Alpaca credential BY
        # DESIGN — a None frame under the promoted `fundamental_like` config, so every session died
        # at `_decide` with `assets are required` as the lane's only journal row (#647).
        #
        # `assets=` (an eager frame) is still honoured when supplied, and is authoritative when
        # given — tests and any caller that already holds reference data mean it.
        #
        # WHEN NEITHER CAN ANSWER, `_decide` DEGRADES LOUDLY instead of raising: venue-only
        # filtering (`asset_universe_mode="all"`) with an ERROR naming the loss. Wider-but-alive is
        # the deliberate trade for the no-metadata state — the raise it replaces was #647's staging
        # blocker — and it is LOUD because ranking funds as stocks is a strategy change, not a
        # degraded lookup. A missing NAME on an individual instrument still EXCLUDES that instrument
        # (kumo-strategies 4d46290: `is_fundamental_like_asset(None, ...) is False`), so a partial
        # metadata outage narrows the universe rather than widening it.
        self._assets = assets
        self._assets_builder = assets_builder
        self._slot = slot
        self._on_result = on_result

    # -- lifecycle, mirroring SessionGateway ------------------------------------------------------
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


    # -- daily-loss stop (#784) -------------------------------------------------------------------

    @staticmethod
    def _daily_loss_module():
        """The installed `daily_loss`, or None on a stack that predates it.

        IMPORTED LOCALLY, NEVER AT MODULE SCOPE. An engine running pre-ea21f98 strategies has no such
        module, and a top-level import would crash-loop the node AT BOOT — a change anchored on
        something absent, at the worst possible moment. Absence must degrade, not detonate.
        """
        try:
            from kumo_strategies.runtime.executor import daily_loss
        except ImportError:
            return None
        return daily_loss

    def _daily_loss_anchor(self) -> dict:
        """`{"equity": <finite>}` or `{}` — the anchor next session's stop reads.

        Returns {} rather than a zero or a null on a stack without the module: an absent key is
        already the module's own way of saying "no usable value", so the two agree.
        """
        mod = self._daily_loss_module()
        if mod is None:
            return {}
        try:
            return mod.anchor(self._broker.equity)
        except Exception:  # noqa: BLE001 — an anchor that cannot be read must not stop a session
            return {}

    async def _persist_halt(self, life, expected) -> None:
        """Persist a risk HALT through the ONE writer, and let a failure surface.

        `lifecycle.halt()` alone survives exactly ONE session: `pgrunner` never persists, and this
        gateway reads lifecycle fresh every session and never writes it back — so a durable stop
        would silently degrade to a one-session decline. `lifecycle_state.save_if_unchanged` is the
        single writer, already used by MOMENTUM and BCTROT (momentum.py:117).

        THE RETURN IS NOT SWALLOWED. `save_if_unchanged` reports failure by RETURNING
        OPERATOR_WON / CONTRADICTION rather than raising, and `enforce` now treats any return other
        than None/True as a failure to persist and journals it. Returning the outcome straight
        through is therefore what makes an unpersisted halt visible instead of reading as persisted.
        """
        from . import lifecycle_state

        outcome = await lifecycle_state.save_if_unchanged(
            self._sm, str(self._strategy_id), life, expected)
        return None if outcome == "SAVED" else outcome

    async def _enforce_daily_loss(self, session: str, life, entered_with):
        """Run the daily-loss stop. Returns a SessionResult when the lane must not trade, else None.

        SAYS SO WHEN IT CANNOT CHECK. A stack without the module, or an unreadable equity, is
        reported rather than passed over — "had no reason to halt" and "has no ability to halt" must
        not be the same empty journal (#548).
        """
        from kumo_strategies.runtime.executor.runner import SessionResult

        mod = self._daily_loss_module()
        if mod is None:
            await self._journal.write(
                "state", "daily-loss stop UNAVAILABLE: the installed kumo_strategies has no "
                         "`daily_loss` module, so this lane cannot halt on a loss",
                session=session, slot=self._slot)
            return None

        verdict = mod.enforce(
            journal=self._journal,
            write=lambda kind, summary, detail=None: self._journal.write(
                kind, summary, session=session, detail=detail, slot=self._slot),
            lifecycle=life,
            equity=self._broker.equity,
            # UNARMED unless explicitly turned on. `None` is the module's "do not enforce", which it
            # journals as NOT ARMED rather than passing silently.
            frac=(getattr(self._limits, "daily_loss_frac", None)
                  if self._daily_loss_armed else None),
            session=session,
            on_halt=lambda _reason: self._persist_halt(life, entered_with),
        )
        verdict = await verdict if inspect.isawaitable(verdict) else verdict
        if getattr(getattr(verdict, "action", None), "name", "") in {"HALT", "BLOCK"}:
            return SessionResult(session=session, state=life.state.value, decided=False,
                                 blocked=f"daily-loss: {getattr(verdict, 'reason', 'refused')}")
        return None

    async def _lifecycle(self):
        from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State
        from kumo_strategies.runtime.executor.store import StrategyState, select

        async with self._sm() as s:
            row = (await s.execute(select(StrategyState).where(
                StrategyState.strategy_id == self._strategy_id))).scalars().first()
        # AN ABSENT ROW IS TRADING (Operator, 2026-08-19). Inverted from "absent means DISABLED".
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

    async def _current_state(self):
        return (await self._lifecycle()).state

    # -- the session ------------------------------------------------------------------------------
    async def run(self, panel, session: str, jobs=None, slot: str | None = None):
        """One rebalance. Returns a `SessionResult`; never raises into the strategy's task.

        `jobs` AND `slot` ARE PART OF THE PROTOCOL AND BOTH DEFAULT. This took `(panel, session)`
        only, while `qc345_rotation._session_coro` calls
        `self._runner.run(panel, day, slot=self._slot_name)` — so every session died on the call with

            TypeError: QC345SessionGateway.run() got an unexpected keyword argument 'slot'

        before deciding anything. Measured 2026-08-24: the alert fired at 14:20:00.107Z, to the
        millisecond, and the lane had not decided since 2026-08-21.

        kumo-strategies wrote this contract down after the SAME failure on 2026-08-17
        (`qc27_runner.py:104`): "`jobs` and `slot` are accepted by EVERY runner even where unused —
        the SessionRunner protocol." Both keep defaults because the OTHER callers pass neither —
        making either required just swaps which caller dies.

        `jobs` is accepted and unused: this gateway has no job runner, and refusing the argument is
        what caused the outage rather than anything about what it does with it.
        """
        if slot:
            # The slot the adapter is filing under wins over the one captured at construction, so a
            # settings-driven slot name reaches the journal rather than the build-time default.
            # `uq_exec_one_decision_per_session` is keyed (strategy_id, session, slot); a swallowed
            # slot makes two decisions in one session collide on one row and lose the second.
            self._slot = slot
        from kumo_strategies.runtime.executor.runner import SessionResult

        life = await self._lifecycle()
        state = life.state

        def _result(**kw):
            return SessionResult(session=session, state=state.value, **kw)

        if not state.decides:
            # DISABLED / WARMUP / HALTED. Not an error and not a warning: this is the normal state of
            # a strategy nobody has armed, and logging it as a failure every month trains an operator
            # to ignore the log.
            return _result(decided=False, blocked=f"lifecycle {state.value}")

        # ADOPT WHAT WE ALREADY HOLD, BEFORE ANYTHING SIZES OFF THE LEDGER (#540). `_claim` fires on
        # ACCEPT, so a book that predates that wiring is held-but-unclaimed — QC345 held five
        # positions and claimed none on 2026-08-29, which is why DELL reads dual-held with one
        # claimant (#541) and why claim-based ceilings could not see this lane at all. Runs here
        # rather than at build: `strategy_positions()` needs a connected, reconciled node, and
        # reconciliation precedes on_start (the same kernel ordering that forced #631's claims to
        # resolve at BUILD rather than lazily). Never raises.
        from strategies.claims_backfill import backfill_claims

        adoption = await backfill_claims(self._broker, self._journal, str(self._strategy_id))
        if adoption.get("status") != "ok":
            # A FAILED SWEEP IS ITS OWN CONDITION, said in the journal where the session's other
            # outcomes are read — not swallowed into a count nobody sees (review, 2026-08-29).
            await self._journal.write(
                "risk", f"claims back-fill unavailable ({adoption.get('error')}) — standing "
                f"positions are unclaimed and invisible to every claim-based ceiling",
                session=session, detail=adoption)
        elif adoption.get("failed"):
            # A WRITE THAT FAILED (a stalled `store.write_claim`, a Postgres error) leaves the
            # position unclaimed and invisible to every claim-based ceiling — the same condition as
            # an unavailable sweep, so the same row kind. A `pool` housekeeping row, or no row at
            # all, is how this read as clean (review, #848).
            failed = adoption["failed"]
            await self._journal.write(
                "risk", f"claims back-fill: {len(failed)} claim write(s) FAILED — those positions "
                f"are unclaimed and invisible to every claim-based ceiling: {', '.join(failed)}",
                session=session, detail=adoption)
        elif (adoption.get("adopted") or adoption.get("capped") or adoption.get("unknowable")
              or adoption.get("refused")):
            await self._journal.write(
                "pool", f"claims back-fill: adopted {adoption['adopted']}, "
                f"capped {len(adoption['capped'])}, unknowable {len(adoption['unknowable'])}, "
                f"refused under lock {len(adoption.get('refused') or [])}",
                session=session, detail=adoption)


        held = set(self._broker.strategy_positions())

        from kumo_strategies.runtime.executor.lifecycle import State

        if state is State.LIQUIDATING:
            # FLATTEN EVERYTHING THIS STRATEGY OWNS, and do not rank at all — the same meaning
            # `PgSessionRunner` gives the state (pgrunner.py:510). Running `decide()` here was the
            # defect: a held name that the model still wanted would stay in `decision.hold` and never
            # appear in `decision.exit`, so a wind-down would quietly leave positions behind while
            # reporting success. LIQUIDATING is an instruction, not an input to the model.
            return await self._liquidate(session, held, _result)

        if await self._journal.decided_this_session(session, self._slot):
            # ALREADY DECIDED — RESUME, do not bail. `on_stop` cancels the session task
            # (qc345_rotation.py:205), and the window between the journal write and the submit loop
            # is real: a pause, a redeploy or a node restart lands there and leaves a session that
            # decided and never traded. Returning here made that permanent — the idempotency key
            # blocks the retry, so the book silently skips a whole monthly rebalance.
            #
            # Replaying is SAFE rather than merely convenient: `OrderRequest.client_order_id` is a
            # hash of (strategy, session, symbol, side), so an order that did go out is denied by
            # Nautilus as a duplicate client order id (strategy.pyx:865) instead of doubling the
            # position. `PgSessionRunner` resumes for the same reason (pgrunner.py:419).
            return await self._resume(session, held, _result)

        # DAILY-LOSS STOP (#784, #548). Placed here on purpose: after the resume block, so a session
        # that already decided replays rather than being halted twice, and BEFORE `_decide`, so
        # nothing is sized or submitted once the lane is over its loss limit.
        #
        # `entered_with` is captured BEFORE `enforce`, because `enforce` halts the lifecycle object
        # and THEN calls `on_halt` — a compare-and-set reading `life.state` afterwards would compare
        # HALTED against HALTED and silently no-op. Same ordering as momentum.py:228.
        if state.may_submit_entries:
            entered_with = life.state
            halted = await self._enforce_daily_loss(session, life, entered_with)
            if halted is not None:
                return halted

        try:
            decision, universe_size, terminal = self._decide(panel, held)
        except Exception as exc:  # noqa: BLE001
            await self._journal.write("error", f"QC345 decision failed: {type(exc).__name__}: {exc}",
                                      session=session, slot=self._slot)
            return _result(decided=False, blocked=f"decision failed: {type(exc).__name__}")

        detail = {
            # THE ANCHOR THE STOP READS NEXT SESSION (#784). Measured before this shipped: QC345-003
            # had 3 decision rows and TECHIVOL-005 had 14, and NOT ONE carried an equity key — so a
            # wired stop would have been ARMED-BUT-UNENFORCEABLE forever, `baseline()` returning
            # MISSING every session. `anchor()` OMITS the key when the value is not finite, so key
            # present <=> value usable.
            **self._daily_loss_anchor(),
            "enter": list(decision.enter), "exit": list(decision.exit), "hold": list(decision.hold),
            "universe": universe_size, "scores": {k: round(v, 6) for k, v in decision.scores.items()},
            # {symbol: bankruptcy|acquisition|unclassified}. Journalled even when empty, because
            # "nothing had delisted" and "we never looked" must not read the same in the record.
            "terminal": terminal,
        }
        if terminal:
            # A terminal holding is REPORTED, not quietly sold. It has stopped printing, so a market
            # order has nothing to hit — submitting one produces a rejection, not an exit, and would
            # look in the journal like an exit that was attempted and refused for an unrelated
            # reason. What an operator needs is to be told the position exists and cannot be closed
            # by the strategy; unwinding it is a corporate-action or manual matter.
            _log.warning(
                "QC345 %s: HELD names have stopped printing and cannot be exited by this strategy "
                "— %s", session, terminal)
            await self._journal.write(
                "risk", f"QC345 {session}: terminal holdings {sorted(terminal)}",
                session=session, detail={"terminal": terminal}, slot=self._slot)
        try:
            # JOURNAL BEFORE ORDERS. A crash between the two must leave evidence that we intended to
            # trade; the reverse order leaves orders nobody can explain. It is also what makes the
            # unique index an idempotency key rather than an audit nicety, and what `_resume` reads
            # to finish a session that was interrupted between the two.
            row_id = await self._journal.write(
                "decision",
                f"QC345 {session}: enter {sorted(decision.enter)} exit {sorted(decision.exit)}",
                session=session, detail=detail, slot=self._slot)
        except Exception as exc:  # noqa: BLE001 — DuplicateDecision and anything else
            return _result(decided=False, blocked=f"journal refused: {type(exc).__name__}")
        if row_id is None:
            # `PgJournal.write` SWALLOWS non-integrity failures and returns None (pgjournal.py:57),
            # so "no exception" is not "durably recorded". Submitting here would place orders with no
            # decision row — invisible to `_resume`, to `explain()`, and to anyone asking why. Pg
            # refuses on the same condition (pgrunner.py:577).
            return _result(decided=False, blocked="decision was not durably journalled")

        if not state.may_submit_exits:
            # SHADOW: the full decision path ran, it is journalled and publishable, and nothing was
            # sent. That is the whole point of SHADOW — it earns trust on live data.
            return _result(decided=True, entered=(), exited=(), held=tuple(sorted(held)),
                           submitted=0, blocked="shadow", detail=detail)

        # LAST LOOK before money moves. The state above was read at the top and deciding takes real
        # time; an operator who halts in that window must not watch it submit anyway.
        state = await self._current_state()
        if not state.may_submit_exits:
            return _result(decided=True, submitted=0,
                           blocked=f"operator moved to {state.value} during the session",
                           detail=detail)

        # RE-READ, NOT CAPTURED AT CONSTRUCTION. An operator edits the target from the UI while the
        # node keeps running; momentum re-reads for the same reason. Falls back to the constructed
        # limits when settings are unreadable — sizing off the account is the old behaviour and a
        # settings hiccup must not change how a live session sizes.
        self._limits = self._limits_for_session()
        # THE PLATFORM'S ANSWER, NOT THE SETTINGS TARGET (#537). One implementation, shared with
        # TECHIVOL — written twice it would be two derivations of one fact, which is the defect it
        # exists to close. Distributes parked capital first, then reads `min(actual, target)`.
        from dataclasses import replace as _replace

        from strategies.session_budget import resolve as _resolve

        _budget = await _resolve(
            self._strategy_id, fallback=getattr(self._limits, "allocated_equity", None),
            session=str(session))
        if _budget is not None and hasattr(self._limits, "allocated_equity"):
            self._limits = _replace(self._limits, allocated_equity=_budget)
        exited, entered, submitted, refusals = await self._submit(decision, session, state, terminal)
        # THE REASON MUST OUTLIVE THE RETURN VALUE. `refusals` carries one specific sentence per name
        # — "no price", "sizing yielded 0 shares at X", the budget gate's own text, the venue's
        # rejection — and it went only into `SessionResult.blocked`. On 2026-08-21 five entries were
        # refused and `exec_action_log` held a decision row and nothing else, so two days later the
        # only way to learn WHY QC345 has never traded was to re-derive it from the source. An
        # operator at 09:31 cannot do that.
        for why in refusals:
            try:
                await self._journal.write("error", f"QC345 {session}: REFUSED {why}",
                                          session=session, slot=self._slot,
                                          detail={"phase": "refusal", "reason": why})
            except Exception as exc:  # noqa: BLE001 — a journal hiccup must not lose the session result
                _log.warning("qc345 could not journal a refusal (%r): %s", exc, why)
        result = _result(decided=True, entered=tuple(entered), exited=tuple(exited),
                         held=tuple(sorted(held)), submitted=submitted,
                         blocked="; ".join(refusals) or None, detail=detail)
        if self._on_result is not None:
            try:
                await self._on_result(result)
            except Exception as exc:  # noqa: BLE001
                # Observers are strictly downstream of trading. A broken notifier costs an alert.
                _log.warning("qc345 session observer failed (ignored): %r", exc)
        return result

    def _decide(self, panel, held: set[str]):
        """The PURE decision. No I/O, no orders — so a failure here cannot half-trade.

        Returns (decision, universe_size, terminal) where `terminal` is {symbol: bucket} for held
        names that have STOPPED PRINTING — read from `terminal_symbols`, never inferred from the
        scanner dropping them.

        Those are two different facts and conflating them breaks in both directions. A name can
        leave the monthly universe while trading perfectly well, and exiting on that alone would
        turn universe churn into forced selling — which upstream fixed deliberately
        (`test_decide_does_not_sell_a_held_name_solely_because_the_source_drops_it`). And a name can
        DELIST while still sitting in a carried-forward universe, so membership says nothing about
        whether it can still be sold.
        """
        from kumo_strategies.strategies.qc345_rotation import QC345ComputedSource, decide

        assets = self._assets
        if assets is None and self._assets_builder is not None:
            # Built at most once per node run: the cache metadata is static after `on_start`. A
            # FAILED build is NOT cached — the next session retries, because "the cache was
            # unreadable this once" must not become "the filter is off for a month".
            try:
                assets = self._assets_builder()
            except Exception as exc:  # noqa: BLE001 — a metadata failure must degrade, not kill the session
                _log.error("QC345: asset metadata unavailable from the cache (%r) — will retry "
                           "next session", exc)
                assets = None
            else:
                self._assets = assets
        cfg = self._cfg
        if assets is None and getattr(cfg, "asset_universe_mode", None) == "fundamental_like":
            # THE #647 DEGRADE. Without this, `QC345ComputedSource.__init__` raises `assets are
            # required for asset_universe_mode='fundamental_like'` and the session dies — measured
            # as every QC345 session's only journal row on staging-ibkr. Venue-only filtering is
            # WIDER (funds rank as stocks), which is a strategy change, so it is said at ERROR —
            # never silently, and never by stopping the lane.
            _log.error("QC345: no asset metadata available — degrading to venue-only filtering "
                       "for this session; the ETF/fund exclusion will NOT run and funds in the "
                       "universe will be ranked as stocks (#647)")
            from dataclasses import replace

            cfg = replace(cfg, asset_universe_mode="all")
        source = QC345ComputedSource(panel, assets, cfg)
        # EVERY READ COMES OFF `source.panel`, NEVER THE RAW ARGUMENT (#385).
        #
        # `QC345ComputedSource.__init__` calls `build_feature_panel` and keeps the result as
        # `self.panel` — that is what adds `eligible`, `momentum`, `liquidity_proxy` and
        # `realized_volatility`. The raw `panel` argument has none of them: it is OHLC and nothing else.
        #
        # This line used to slice `day` out of the raw argument while `universe` was read off the
        # featurized one, so `decide()` -> `select_portfolio` -> `scoped["eligible"]` raised KeyError.
        # QC345's first live rebalance, 2026-08-20 13:35:00 UTC, died there and booked nothing.
        #
        # It read as correct because the featurization is a SIDE EFFECT of constructing the source, the
        # line above it works off that source, and both frames are called `panel`. The step was never
        # missing — it was assigned where the next line did not look.
        featurized = source.panel
        session_date = featurized["date"].max()
        universe = source.eligible(session_date)
        day = featurized.loc[featurized["date"] == session_date]
        terminal = source.terminal_symbols(session_date, held)
        return decide(day, self._cfg, held, universe=universe), len(universe), terminal


    async def _claim(self, symbol: str, qty: int, entry_px: float) -> None:
        """Record what this lane now holds (#540). NEVER RAISES.

        CLAIMED ON ACCEPT, NOT ON FILL, matching `pgrunner`'s `if r.ok` — and matching what
        `qc27_runner` does for TECHIVOL, so the two lanes cannot disagree about when a claim exists.
        An over-claim only narrows OTHER lanes' ceilings; an under-claim lets them size into a
        position we are holding. Safe direction chosen deliberately.

        The ledger is bookkeeping and the order is already at the venue, so a failure here must not
        abandon a rotation mid-flight with sells already away (#377).
        """
        try:
            from kumo_strategies.runtime.executor.store import record_claim

            await record_claim(self._journal, self._strategy_id, symbol, qty, entry_px)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not stop a session
            _log.warning("%s: claim for %s not recorded (%r) — other lanes may size into it",
                         self._strategy_id, symbol, exc)

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

    async def _release(self, symbol: str) -> None:
        """Drop the claim on a FULL exit (#540). NEVER RAISES.

        QC345 exits whole positions — `qty = abs(positions.get(symbol))` — so the claim goes rather
        than shrinks. A PARTIAL exit would need `record_claim` with the REMAINDER instead: dropping on
        a partial leaves the lane not claiming shares it still holds, which is the same hole one level
        down.
        """
        try:
            from kumo_strategies.runtime.executor.store import drop_claim

            await drop_claim(self._journal, self._strategy_id, symbol)
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s: claim for %s not released (%r) — it will keep narrowing other lanes",
                         self._strategy_id, symbol, exc)

    async def _intent(self, session: str, side: str, qty: int, symbol: str) -> bool:
        """Journal an order's INTENT before the broker sees it. False means do not send (#513).

        Every other lane does this (`qc27_runner._send`, pgrunner); QC345 wrote no order row at all,
        so `/slots` — which counts ORDER rows — reported `DECIDED, NEVER ATTEMPTED` for this lane
        STRUCTURALLY, whatever it had actually done. On 2026-08-24 that verdict was read as evidence
        of a trading failure when it was measuring nothing. Success and silence were indistinguishable.

        FAIL CLOSED. `PgJournal.write` swallows non-integrity failures and returns None, so "no
        exception" is not "durably recorded" — the same reason the decision row is checked for None
        above. An order sent without a record is invisible to `/slots`, to `explain()`, and to
        `_resume`, which decides what was attempted from exactly these rows and would re-send it.
        """
        try:
            wrote = await self._journal.write(
                "order", f"{side} {qty} {symbol}: submitting",
                session=session, symbol=symbol, detail={"phase": "intent"}, slot=self._slot)
        except Exception as exc:  # noqa: BLE001
            _log.warning("qc345 intent not journalled for %s %s (%r) — not sending", side, symbol, exc)
            return False
        if wrote is None:
            # GUARDED TOO. The intent write above is wrapped and this one was not, which made the
            # FAILURE path more dangerous than the success path: `_submit` is awaited unguarded
            # (qc345.py:351), so an exception here leaves `run()` mid-loop — and EXITS RUN FIRST, so
            # the sells are already away when the entries are abandoned. A half-executed rotation is
            # exactly what that ordering exists to prevent, reached through the error handler.
            #
            # Reachable precisely when it matters: `PgJournal.write` swallows non-integrity failures
            # and returns None, so an unavailable store gives None here and raises on the next call.
            try:
                await self._journal.write(
                    "error", f"{side} {symbol}: intent not journalled — not sending",
                    session=session, symbol=symbol, slot=self._slot)
            except Exception as exc:  # noqa: BLE001
                _log.warning("qc345 could not journal the refusal for %s %s (%r)", side, symbol, exc)
            return False
        return True

    async def _submit(self, decision, session: str, state, terminal: dict | None = None):
        """Orders. EXITS FIRST, and that ordering is a risk decision rather than a stylistic one.

        Selling before buying frees both cash and Alpaca's share reservation before anything asks for
        them, so a rotation cannot be half-applied into a cash shortfall — left holding what it meant
        to sell AND unable to buy what it meant to enter.
        """
        from kumo_strategies.runtime.executor.broker import OrderRequest

        entered: list[str] = []
        exited: list[str] = []
        refusals: list[str] = []
        submitted = 0
        positions = self._broker.strategy_positions()

        terminal = terminal or {}
        for symbol in sorted(decision.exit):
          # ONE BAD SYMBOL REFUSES ONE SYMBOL. Everything below can raise — the broker call, the
          # price read, the budget gate — and `run()` awaits `_submit` UNGUARDED (qc345.py:351).
          # Without this, a transient failure on one name escapes with the earlier sells already at
          # the venue and the buys never attempted: the strategy flattens its book and does not
          # rebuy, from an error that refused nothing and reported nothing. Every other failure
          # mode in these loops appends to `refusals` and continues; that asymmetry was the defect
          # (codex, 2026-08-25). Same family as the PEAK trim path on 2026-08-12, where a partial
          # sequence was allowed to half-execute and stripped protection off five live positions.
          try:
            if symbol in terminal:
                # Do not send a sell into a name that has stopped printing. The order cannot fill,
                # and a rejection in the log is indistinguishable from an exit refused for an
                # ordinary reason — which is how a stuck position stops being visible.
                refusals.append(f"exit {symbol}: terminal ({terminal[symbol]}) — cannot be sold")
                continue
            # LONG-ONLY BY CONTRACT (#950): a SHORT here would be sized as a long and SOLD — doubled,
            # not closed. QC345 never holds one (POSITION_SIDE is LONG); the abs() is a magnitude for
            # a position whose side the lane already knows, and it must not be copied to a lane that
            # does not.
            qty = int(abs(positions.get(symbol, 0)))
            if qty <= 0:
                continue
            if not await self._intent(session, "SELL", qty, symbol):
                refusals.append(f"exit {symbol}: intent not journalled")
                continue
            # `.exit()`, NOT `.submit()` (#459). `exit` calls `feed.release_for_exit(...)` to cancel
            # the resting protective stop before selling; `submit` releases nothing. Every held symbol
            # at the venue has `qty_available = 0` -- 100% of shares reserved by their stops (AEM 18/0,
            # CGAU 174/0, BETA 79/0) -- so a SELL sent through `submit` is refused outright with
            # `403 insufficient qty available (available: 0)`. QC345 had ZERO calls to `exit`, so every
            # exit it ever attempted was unsellable, while `pgrunner` and `qc27_runner` both route here.
            res = await self._broker.exit(OrderRequest(symbol=symbol, side="SELL", qty=qty,
                                                       session=session, strategy_id=self._strategy_id))
            if getattr(res, "ok", False):
                await self._release(symbol)
                exited.append(symbol)
                submitted += 1
            else:
                # `.detail`, not `.reason` — `OrderResult` is (ok, order_id, detail, request)
                # (broker.py:43). Reading a field that does not exist made every refusal read
                # "refused", discarding the one thing that says WHY: "not a subscribed instrument",
                # "no instrument definition cached", the venue's own rejection text.
                refusals.append(f"exit {symbol}: {getattr(res, 'detail', 'refused')}")
          except Exception as exc:  # noqa: BLE001 — refuse this symbol, never the rotation
            refusals.append(f"exit {symbol}: raised {exc!r} — refused, rotation continues")

        if not state.may_submit_entries:
            # LIQUIDATING exits and never enters. Reached here rather than skipped earlier so the
            # exits above still run — a wind-down that could not sell would be the worst of both.
            return exited, entered, submitted, refusals

        for symbol in sorted(decision.enter):
          # Guarded for the same reason as the exits above, and this is the loop that matters most:
          # the sells are ALREADY AWAY by the time it runs.
          try:
            price = self._broker.last_price(symbol)
            if not price or price <= 0:
                refusals.append(f"enter {symbol}: no price")
                continue
            qty = int(min(self._limits.max_position_notional, self._equity_per_position()) // price)
            if qty <= 0:
                refusals.append(f"enter {symbol}: sizing yielded 0 shares at {price}")
                continue
            allowed, why = await self._budget_allows(symbol, qty * price)
            if not allowed:
                refusals.append(f"enter {symbol}: {why}")
                continue
            if not await self._intent(session, "BUY", qty, symbol):
                refusals.append(f"enter {symbol}: intent not journalled")
                continue
            res = self._broker.submit(OrderRequest(symbol=symbol, side="BUY", qty=qty,
                                                   session=session, strategy_id=self._strategy_id))
            if getattr(res, "ok", False):
                await self._claim(symbol, qty, price)
                entered.append(symbol)
                submitted += 1
            else:
                refusals.append(f"enter {symbol}: {getattr(res, 'detail', 'refused')}")
          except Exception as exc:  # noqa: BLE001 — refuse this symbol, never the rotation
            refusals.append(f"enter {symbol}: raised {exc!r} — refused, rotation continues")
        return exited, entered, submitted, refusals

    async def _liquidate(self, session: str, held: set[str], _result):
        """LIQUIDATING: sell everything this strategy owns. No ranking, no entries.

        Journalled like any other decision so the session leaves a record, but the decision is the
        state itself rather than the model's — which is why `decide()` is never called.
        """
        from kumo_strategies.runtime.executor.broker import OrderRequest

        await self._journal.write(
            "decision", f"QC345 {session}: LIQUIDATING — flatten {sorted(held)}",
            session=session, detail={"liquidating": True, "exit": sorted(held)}, slot=self._slot)
        positions = self._broker.strategy_positions()
        exited, refusals, submitted = [], [], 0
        for symbol in sorted(held):
            # LONG-ONLY BY CONTRACT (#950): a SHORT here would be sized as a long and SOLD — doubled,
            # not closed. QC345 never holds one (POSITION_SIDE is LONG); the abs() is a magnitude for
            # a position whose side the lane already knows, and it must not be copied to a lane that
            # does not.
            qty = int(abs(positions.get(symbol, 0)))
            if qty <= 0:
                continue
            # THE WIND-DOWN PATH JOURNALS ITS INTENT TOO (#513, second half, codex 2026-08-25).
            # This bypassed `_intent` entirely: one decision row naming the symbols, then sells
            # straight to the broker. A decision row says what was INTENDED, not what was SENT, so
            # a submit that never happened and one that was refused looked identical afterwards —
            # which is the distinction `/slots` and `_resume` are built on, and the exact defect
            # #513 was filed about. Fail-closed like every other intent: no record, no order.
            if not await self._intent(session, "SELL", qty, symbol):
                refusals.append(f"exit {symbol}: intent not journalled")
                continue
            # `.exit()`, NOT `.submit()` (#459). `exit` calls `feed.release_for_exit(...)` to cancel
            # the resting protective stop before selling; `submit` releases nothing. Every held symbol
            # at the venue has `qty_available = 0` -- 100% of shares reserved by their stops (AEM 18/0,
            # CGAU 174/0, BETA 79/0) -- so a SELL sent through `submit` is refused outright with
            # `403 insufficient qty available (available: 0)`. QC345 had ZERO calls to `exit`, so every
            # exit it ever attempted was unsellable, while `pgrunner` and `qc27_runner` both route here.
            res = await self._broker.exit(OrderRequest(symbol=symbol, side="SELL", qty=qty,
                                                       session=session, strategy_id=self._strategy_id))
            if getattr(res, "ok", False):
                await self._release(symbol)
                exited.append(symbol)
                submitted += 1
            else:
                refusals.append(f"exit {symbol}: {getattr(res, 'detail', 'refused')}")
        return _result(decided=True, exited=tuple(exited), held=tuple(sorted(held)),
                       submitted=submitted, blocked="; ".join(refusals) or None,
                       detail={"liquidating": True})

    async def _resume(self, session: str, held: set[str], _result):
        """Finish a session that decided and did not (finish) submitting.

        Reads the decision back out of the journal rather than recomputing it. Recomputing would be
        a DIFFERENT decision — prices have moved since — so the orders that go out would not be the
        ones the journal says were authorised, which is the one property the journal exists to give.
        """
        row = None
        try:
            row = await self._journal.explain(session)
        except Exception as exc:  # noqa: BLE001
            _log.warning("qc345 resume could not read the journalled decision: %r", exc)
        detail = (row or {}).get("detail") or {}
        if not detail:
            # Decided, but nothing readable to replay. Say so instead of reporting a quiet success:
            # this is the case where a human needs to look.
            return _result(decided=False,
                           blocked=f"{session}/{self._slot} already decided and cannot be replayed")

        state = await self._current_state()
        if not state.may_submit_exits:
            return _result(decided=True, blocked=f"resume skipped — lifecycle {state.value}",
                           detail=detail)
        replay = SimpleNamespace(enter=tuple(detail.get("enter") or ()),
                                 exit=tuple(detail.get("exit") or ()),
                                 hold=tuple(detail.get("hold") or ()), scores={})
        exited, entered, submitted, refusals = await self._submit(replay, session, state)
        return _result(decided=True, entered=tuple(entered), exited=tuple(exited),
                       held=tuple(sorted(held)), submitted=submitted,
                       blocked="; ".join(["resumed", *refusals]), detail=detail)

    def _limits_for_session(self):
        """`self._limits` with this strategy's CURRENT allocation. Never raises."""
        from dataclasses import replace

        target = _allocated_equity()
        if target is None:
            return self._limits
        if "allocated_equity" not in getattr(type(self._limits), "__dataclass_fields__", {}):
            # THE FIELD MAY NOT EXIST. This repo pins kumo-strategies by revision, and an unguarded
            # `replace()` would raise TypeError mid-session, after the decision is journalled and
            # before a single order — the silent-death shape that has cost this platform two sessions
            # already. Same guard momentum carries, for the same reason.
            #
            # `getattr(..., {})` and not `type(x).__dataclass_fields__`: the direct form raises
            # AttributeError on anything that is not a dataclass, which makes the guard itself the
            # mid-session death it exists to prevent. Six existing tests found that within a minute of
            # it being written — their `_limits` is a SimpleNamespace, and a guard that only survives
            # inputs it approves of is not a guard.
            _log.warning("qc345: installed RiskLimits has no `allocated_equity` — sizing off the "
                         "account, as before. Pin kumo-strategies to a revision that has it.")
            return self._limits
        return replace(self._limits, allocated_equity=target)

    def _equity_per_position(self) -> float:
        """This strategy's ALLOCATION divided across its book — not the account's.

        QC345 had never placed an order. On 2026-08-21 it decided to enter INTC, MRVL, DELL, LRCX and
        AMAT and submitted nothing, because this line read the ACCOUNT's ~103,466 while its allocation
        is 20,000: 103,466 x 0.80 / 5 = 16,554 a name, of which AT MOST ONE fits a 20,000 sleeve. The
        researched five-name equally-weighted book cannot be built at any price, and the budget gate
        refuses the rest at submit — "buys the same and gets rejected", exactly as
        `RiskLimits.allocated_equity`'s own docstring predicted before this happened.

        `pgrunner`, `qc27_runner` and `template_runner` all read the allocation here. This was the one
        runner that did not, and it is the one that never traded.

        `is not None` rather than `or`, deliberately: 0 is BOTH the schema default and an operator
        saying "wind down", and `x or y` treats 0.0 as falsy — so the one strategy being wound down
        would size off the ENTIRE account. Zero allocation means zero per position.
        """
        allocated = getattr(self._limits, "allocated_equity", None)
        equity = allocated if allocated is not None else (self._broker.equity() or 0.0)
        return (float(equity) * self._limits.max_deployed_frac) / max(self._cfg.portfolio_size, 1)

    async def _budget_allows(self, symbol: str, notional: float) -> tuple[bool, str]:
        """Ask the SAME predicate the exec client enforces at submit (#320).

        Not a second budget rule. `budget_gate.may_submit` is imported here rather than reimplemented
        because two derivations of one limit disagree, and the disagreement would look like a working
        strategy quietly holding more than it was granted. This call is the EARLY, explainable
        refusal — it puts "over budget" in the session result where an operator reads it, instead of
        letting the order reach the venue and come back as a bare rejection.
        """
        try:
            from api.budget_gate import may_submit
            from api.budget_store import load_book
            from api.db import session_factory

            # `load_book` takes a DB SESSION (budget_store.py:66). Calling it bare raised TypeError
            # into the broad except below, so this pre-check silently permitted every order and the
            # test that claimed it "fails open" was passing on the wrong exception.
            async with session_factory() as db:
                book = await load_book(db)
            sleeve = book.sleeves.get(self._strategy_id)
            decision = may_submit(sleeve, is_entry=True, notional=notional,
                                  currently_deployed=self._deployed())
            return bool(decision.allowed), decision.reason
        except Exception as exc:  # noqa: BLE001
            # FAILS OPEN, matching the exec client. The gate is enforced again at submit, so a
            # failure here costs a clearer error message, not a control. Failing closed would let a
            # Postgres blip silently stop a funded strategy from trading for a month.
            _log.warning("qc345 budget pre-check unavailable (%r) — deferring to the exec client", exc)
            return True, ""

    def _deployed(self) -> float:
        entries = self._broker.position_entries()
        positions = self._broker.strategy_positions()
        return sum(abs(positions.get(s, 0)) * p for s, p in entries.items())


def build_qc345_strategy(*, feed=None):
    """QC345-003 wired to `QC345SessionGateway`, or None when the gate is off.

    RAISES when the gate is ON but the wiring is incomplete, rather than returning None. A strategy
    that is switched on and silently absent is the worst outcome available: the operator sees the
    flag set, the node comes up clean, and nothing trades or says why.

    The one invariant this function exists to hold: `session_runner` is ALWAYS passed. Without it the
    adapter decides and submits inside `_decide_for`, bypassing lifecycle, journal, risk and budget
    entirely. There is no code path here that constructs the strategy without it, and a test asserts
    that by reading this source.
    """
    if not _enabled():
        return None

    import asyncio

    from kumo_strategies.runtime.calendar import build_calendar
    from kumo_strategies.runtime.executor.pgjournal import PgJournal
    from kumo_strategies.runtime.executor.runner import RiskLimits
    from kumo_strategies.runtime.executor.store import create_all, make_engine, make_sessionmaker
    from kumo_strategies.runtime.nautilus.broker import NautilusBroker
    from kumo_strategies.runtime.nautilus.qc345_rotation import QC345RotationStrategy


    async def _prepare():
        # cross_loop: this runs inside a short-lived asyncio.run() during synchronous node startup,
        # while every later query runs on the TradingNode's loop. A pooled AsyncEngine would hand out
        # connections bound to a loop that no longer exists.
        eng = make_engine(None, cross_loop=True)
        await create_all(eng)
        sm = make_sessionmaker(eng)
        # SUBSCRIBE WHAT WE HOLD, IN THIS SAME `asyncio.run` — see `_held_claims`. A second
        # `asyncio.run` fails with "no running event loop"; the cross-loop engine is built for the
        # node's loop, not a throwaway one.
        return sm, await _held_claims(sm, STRATEGY_ID)

    # UNIVERSE FIRST, before anything opens a database engine. Every refusal should cost as little
    # as possible and say as much as possible: an operator who set the gate and forgot the universe
    # should get that sentence, not a connection error from a store the strategy never needed.
    symbols = _universe_symbols()
    if not symbols:
        raise RuntimeError(
            "strategies.QC345_ENABLED is on but `strategies.QC345_UNIVERSE` is empty — refusing "
            "to register a strategy that can never decide. QC345 ranks cross-sectionally, so an "
            "empty universe is not a quiet no-op. Set the universe in settings, or unset the gate.")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        sm, held = asyncio.run(_prepare())
        symbols = sorted(set(symbols) | held)
    else:  # pragma: no cover — build runs before the node's loop exists
        raise RuntimeError(
            "build_qc345_strategy must run before the node's event loop starts — it opens a "
            "cross-loop engine for the lifecycle and journal stores")

    cfg = _live_config()
    from strategies.momentum import _lane_symbols

    symbols = _lane_symbols(symbols)
    broker = NautilusBroker(strategy=None, instrument_ids=None)   # resolved by the strategy (#622)
    # The delisting ledger is the EXEC provider's declared capability (#647), stamped onto the feed
    # by build_node — None on a venue without one, which `_reference_status` reports LOUDLY as
    # "delisting detection OFF" rather than treating as clean. Read eagerly (one bulk call at build,
    # exactly the old cost) because a ledger that is down at build time should say so at build time.
    status_by_symbol = _reference_status(getattr(feed, "_reference_assets", None))
    # The NAME/classification half waits for the cache: instruments exist only after the adapter
    # connects and the strategy resolves them at `on_start` (#622), both of which happen before the
    # first session `_decide` — the only consumer. `broker.strategy` is None here and is assigned by
    # the strategy when it registers, which is why the builder closes over the broker, not a value.
    assets_builder = lambda: _assets_frame_from_cache(  # noqa: E731 — a named def would suggest reuse
        broker.strategy, symbols, status_by_symbol=status_by_symbol)
    # ONE read, both halves. `slot` names the row; `offset` schedules the alert. Passing only the
    # second is what let an operator move the decision time while the journal kept saying "open+5m".
    slot, offset_minutes = _slot_and_offset_from_settings()
    # ALLOCATION AT CONSTRUCTION, and re-read per session in `_limits_for_session`. A bare
    # RiskLimits is what qc27
    # already avoids (`allocated_equity=ALLOCATED_EQUITY`) and momentum re-reads every session — QC345
    # passed neither, so `_equity_per_position` had nothing to read even once it knew to look. Setting
    # a field nobody reads and reading a field nobody sets are the same defect twice.
    gateway = QC345SessionGateway(sm, PgJournal(sm, strategy_id=STRATEGY_ID), cfg, broker,
                                  RiskLimits(allocated_equity=_allocated_equity(), max_deployed_frac=1.0),
                                  assets_builder=assets_builder, slot=slot)
    strategy = QC345RotationStrategy(
        # LIVE SLOT RE-READ (#514), passed only if the INSTALLED adapter accepts it (db25413).
        **_settings_cache._live_reread_kwargs(
            QC345RotationStrategy, read_open_offset=_read_open_offset),
        cfg=cfg,
        # The adapter's own source is unused on this path — the gateway computes the universe per
        # session from the panel it is handed. It is still the RIGHT source rather than a stub,
        # because a constructor argument that is wrong-but-unreached becomes wrong-and-reached the
        # first time someone removes a branch. Built WITHOUT an assets frame: at build time the
        # cache holds no instruments yet (#624), so `_adapter_source` takes its documented
        # no-reference fallback (asset_universe_mode="all") for this never-consulted object.
        source=_adapter_source(cfg),
        symbols=symbols,
        # require_exchange: this path can place orders, so the holiday-unaware fallback is refused
        # rather than silently accepted.
        # The venue's OWN sessions when its adapter supplies them (#628), else Alpaca as before.
        # `require_exchange=True` is UNCHANGED and still refuses the holiday-unaware fallback — an
        # injected calendar SATISFIES that requirement rather than relaxing it.
        calendar=build_calendar(
            require_exchange=True,
            calendar=getattr(feed, "_venue_calendar", None),
        ),
        history_days=420,  # 254 warmup sessions plus slack, in CALENDAR days
        session_runner=gateway,          # <- the whole point of this module
        order_id_tag=ORDER_ID_TAG,
        # CLAIMS NOTHING, deliberately. `external_order_claims` are EXCLUSIVE node-wide and raise
        # InvalidConfiguration at `Trader.add_strategy`; QC345 shares its universe with MOMENTUM and
        # BCTROT by construction, so any overlap stops the node booting. QC345 holds nothing yet, so
        # it has no reconciliation-generated flatting order to adopt.
        external_order_claims=None,
        # WHEN IT DECIDES, from settings (#360). QC345 schedules a single alert from an OPEN OFFSET IN
        # MINUTES rather than from a slot string, so only `open+Nm` is expressible here — `close-20m`
        # has nowhere to go on this strategy and is refused rather than silently rounded to something
        # else. Falling back to the built-in offset on anything unusable, because a strategy that never
        # decides is a worse outcome than one deciding at its default time, and nothing announces it.
        open_offset_minutes=offset_minutes,
    )
    from api.venue_preference import prefer_primary_exchange  # noqa: PLC0415

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
    # is kumo-strategies 53e2ff5, which now sends SELLs through `exit()` rather than `submit()`.)
    broker.feed = feed
    return strategy


def _allocated_equity() -> float | None:
    """QC345's configured allocation, or None when it cannot be read.

    None, never a guess: `_equity_per_position` falls back to the account only when the allocation is
    genuinely unknown, and an unreadable settings store must not silently change how a live session
    sizes. Read at build AND per session — an operator edits the target from the UI while the node
    keeps running, which is why momentum re-reads it too.
    """
    try:
        from api.settings import resolve

        target = resolve("strategies").get(STRATEGY_ID)
    except Exception as exc:  # noqa: BLE001
        _log.warning("qc345 allocation unreadable (%r) — sizing off the account, as before", exc)
        return None
    return None if target is None else float(target)


def _live_config():
    """The PROMOTED candidate, read from the generated `qc345` settings domain (#318).

    Not the dataclass defaults. `momentum_price_field="close"` and a 252-session corporate-action
    window are the LIVE-FEASIBLE pair — adjusted prices do not exist live, and the engine refuses raw
    close unless the split window covers the whole momentum lookback (#319).
    """
    from kumo_strategies.strategies.qc345_rotation import QC345RotationConfig

    from api import settings

    values = settings.resolve("qc345")
    return _build_config(QC345RotationConfig, values)


def _build_config(cls, values: dict):
    """Settings dict -> config dataclass, nested groups included.

    Unknown keys are DROPPED rather than raising: the schema is generated from the dataclass, so an
    unknown key means a stale values row from before a field was removed, and refusing to start over
    one is worse than ignoring it.
    """
    import typing
    from dataclasses import fields, is_dataclass

    # RESOLVE THE ANNOTATIONS (#945). `f.type` is the RAW annotation: under a forward reference or
    # `from __future__ import annotations` it is the STRING "MarketViewConfig", `is_dataclass(str)` is
    # False, and the settings dict was passed through untouched — the dataclass accepted it and the
    # first reader of the field (`cfg.market_view.signal`, the market-aware hook) raised on every poll
    # on paper 2026-09-11. `exits` had the same annotation shape on the running pin and reached the
    # engine as a dict too; nothing read it at decision time, which is the only reason it was quiet.
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name not in values:
            continue
        kwargs[f.name] = _coerce_setting(hints.get(f.name, f.type), values[f.name])
    return cls(**kwargs)


def _coerce_setting(hint, raw):
    """One settings value → the type its field declares.

    Settings carry an Enum field as its VALUE STRING ("none", "exit_only"): the schema is generated
    with `enum: [m.value …]`, the file stores strings, and `resolve()` hands them back as strings. A
    dataclass accepts the string silently; `MarketViewConfig.__post_init__` then raises
    `'str' object has no attribute 'name'` on the first f-string that names the member (#945, found by
    the red test once the dict half was fixed). `Optional[Enum]` (`action: MarketAction | None`) is
    unwrapped; None stays None; a value the Enum does not know RAISES here, named, rather than being
    stored as a string that every reader downstream mistakes for a member.
    """
    import enum
    import types
    import typing
    from dataclasses import is_dataclass
    if is_dataclass(hint) and isinstance(raw, dict):
        return _build_config(hint, raw)
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        members = [a for a in typing.get_args(hint) if a is not type(None)]
        if raw is None:
            return None
        if len(members) == 1:
            return _coerce_setting(members[0], raw)
        return raw
    if isinstance(hint, type) and issubclass(hint, enum.Enum) and isinstance(raw, str):
        try:
            return hint(raw)
        except ValueError:
            raise ValueError(f"settings value {raw!r} is not a member of {hint.__name__} "
                             f"({[m.value for m in hint]})") from None
    return raw


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
    """What the node SUBSCRIBES to and QC345 ranks across — from settings, `strategies.QC345_UNIVERSE`.

    NOT DERIVED, AND THAT IS THE HONEST STATE. The researched universe is ~50 names selected from
    thousands by liquidity and market-cap proxy. The source split (kumo-strategies#42) is what makes
    that selection reachable live — but reaching it still requires daily bars for the pre-selection
    set, which means a batch fetch cockpit does not do yet. `TradableUniverse().symbols()` is the
    whole tradable market and `asset()` is one HTTP call per symbol; neither is a universe you can
    subscribe a TradingNode to.

    So an operator supplies the list, and an empty one BLOCKS STARTUP rather than degrading. Picking
    a convenient substitute — MOMENTUM's ~97-name BCT pool, say — would run QC345 over a universe it
    was never researched on and produce a number nobody could interpret. The fidelity check upstream
    (live top-50 == backtest top-50 on the same bars) exists precisely to catch that, and quietly
    failing it is worse than not starting.
    """
    from api import settings

    from api.lane_universes import universe_symbols

    # ONE reader with the IB connector's `load_contracts` (#871).
    return universe_symbols(settings.resolve("strategies"), "QC345_UNIVERSE")


def _reference_status(reference_assets):
    """symbol -> status from the exec provider's DECLARED reference ledger, or None. Never raises.

    THREE STATES, NEVER TWO (#647). `reference_assets` is the capability the exec provider declared
    on its spec (`ExecClientSpec.reference_assets`, stamped onto the feed by `build_node`):

      * a callable that answers  -> delisting detection ARMED (the dict below feeds the `status`
        column that `terminal_buckets` reads by name);
      * a callable that RAISES   -> ledger unreadable, its own loud condition — detection OFF for
        this node run, said at ERROR;
      * None                     -> this venue HAS no reference ledger (IBKR) — detection OFF, said
        at ERROR, because a held name that delists will keep reading "active" and silence here is
        how that stays invisible for a month.

    The lane never learns which vendor answered: the Alpaca provider serves this from ITS OWN
    configured base URL; the hardcoded `paper-api.alpaca.markets` + APCA credential read that used
    to sit here is what #647 measured — it did not follow the account, and its absence killed every
    staging-ibkr session at `_decide`.
    """
    if reference_assets is None:
        _log.error("QC345: no reference-asset ledger on this venue — delisting detection is OFF; "
                   "a held name that delists will keep reading active (#647)")
        return None
    try:
        rows = reference_assets()
    except Exception as exc:  # noqa: BLE001 — a broken ledger must degrade loudly, not stop the build
        _log.error("QC345: reference-asset ledger unreadable (%r) — delisting detection is OFF "
                   "for this node run (#647)", exc)
        return None
    return {r["symbol"]: r.get("status") for r in rows if r.get("symbol")}


def _assets_frame_from_cache(strategy, symbols: list[str], status_by_symbol: dict | None = None):
    """symbol / name / exchange (/status) from the instruments the node ALREADY holds. None only
    when there is nothing to read — and that None is loud one level up (`_decide` degrades).

    NO HTTP CALL AND NO VENDOR (#624). `Instrument.info` is Nautilus's documented slot for venue
    metadata and both adapters populate it — IBKR with the full `contract_details_to_dict` (an
    EXPLICIT `stockType` classification plus `longName`), Alpaca with the asset's `name`. The
    venue-neutral `asset_meta` accessor (api/providers/asset_meta.py) is the one place that knows
    both shapes; this frame never learns which broker answered.

    Three outcomes per symbol, none silent:
      * classified by the venue as ETF/FUND  -> excluded HERE (row dropped), logged — an explicit
        classification beats the engine's name heuristic;
      * named                                -> the row travels and kumo-strategies'
        `is_fundamental_like_asset` decides, exactly as before;
      * NO name and NO classification        -> UNCLASSIFIABLE: named at ERROR, and the row travels
        with name=None, which the engine EXCLUDES (kumo-strategies 4d46290: a missing name is
        refused, never admitted) — reported and excluded, never silently ranked.

    `status` comes from `_reference_status` when the venue declared a ledger. Without one the column
    is ABSENT, not fabricated: `terminal_buckets` reads the column by NAME and its own
    "no status column" branch is the honest best-effort degrade — counterfeiting `status="active"`
    would render absence as an answer.

    An EMPTY resolution returns None rather than an empty frame: empty IS the bug (2026-08-29) — an
    empty frame would quietly exclude the whole universe while looking like reference data.
    """
    if strategy is None or getattr(strategy, "cache", None) is None:
        _log.error("QC345: no resolved strategy/cache to read asset metadata from — the assets "
                   "frame cannot be built")
        return None
    iids = {i.symbol.value: i for i in (getattr(strategy, "_iids", None) or [])}
    if not iids:
        _log.error("QC345: the strategy has resolved NO instruments — the assets frame cannot be "
                   "built from an empty cache view")
        return None

    from api.providers.asset_meta import asset_meta

    rows, unclassifiable, venue_classified_out = [], [], []
    for s in symbols:
        iid = iids.get(s)
        inst = strategy.cache.instrument(iid) if iid is not None else None
        meta = asset_meta(inst)
        if meta["name"] is None and meta["asset_type"] is None:
            unclassifiable.append(s)
        if meta["asset_type"] in ("ETF", "FUND"):
            venue_classified_out.append(s)
            continue
        rows.append({"symbol": s, "name": meta["name"], "exchange": meta["exchange"]})
    if venue_classified_out:
        _log.info("QC345: %d universe symbols excluded by the venue's OWN classification "
                  "(ETF/FUND): %s", len(venue_classified_out),
                  ", ".join(sorted(venue_classified_out)[:20]))
    if unclassifiable:
        # Named, not counted, at ERROR: these carry no name and no classification in the venue's
        # instrument metadata, so they CANNOT be ranked — the engine refuses a nameless row.
        _log.error("QC345: %d universe symbols are UNCLASSIFIABLE (no name, no classification, in "
                   "this venue's instrument metadata) and will not be ranked: %s",
                   len(unclassifiable), ", ".join(sorted(unclassifiable)[:20]))
    if status_by_symbol is not None:
        unknown = [s for s in symbols if s not in status_by_symbol]
        if unknown:
            # A symbol the reference ledger has never heard of cannot have its delisting noticed.
            _log.warning("QC345: %d universe symbols are unknown to the reference-asset ledger: %s",
                         len(unknown), ", ".join(sorted(unknown)[:20]))
        for row in rows:
            row["status"] = status_by_symbol.get(row["symbol"])

    import pandas as pd

    # Explicit columns: an all-excluded universe must still yield a frame the engine can read
    # (`filter_asset_universe` checks columns by name), not a shapeless empty one.
    cols = ["symbol", "name", "exchange"] + (["status"] if status_by_symbol is not None else [])
    return pd.DataFrame(rows, columns=cols)


def _adapter_source(cfg, assets=None):
    """A source for the adapter's constructor, built from the SAME assets frame the gateway uses.

    THIS RAISED IN PRODUCTION AND CRASH-LOOPED THE ENGINE. The first version passed `assets=None` on
    the reasoning that the live path never reads this source — the gateway computes the universe from
    each session's panel — and the docstring claimed it was "the right type rather than a stub".

    It was neither. `QC345ComputedSource.__init__` calls `filter_asset_universe` eagerly, which
    RAISES `ValueError: assets are required for asset_universe_mode='fundamental_like'` under the
    promoted config. So the object could not be constructed at all, `build_qc345_strategy` raised
    inside `build_node`, and the engine restarted in a loop the moment QC345 was enabled.

    Two things hid it, and both are worth remembering:

      * Every test built a config through a fixture, and the failure needs the PROMOTED config —
        `asset_universe_mode="fundamental_like"`. A test that constructs its own config is testing
        its own config.
      * The deploy check passed. `/trades` reported `status: ok` with all 8 positions because the
        API serves the last-good frame out of Redis, which survives the engine dying. A crash-looping
        engine and a healthy one look identical from the REST boundary — the restart count and the
        engine's own log are the only places the difference shows.
    """
    import pandas as pd
    from kumo_strategies.strategies.qc345_rotation import QC345ComputedSource

    # The frame must carry the column `momentum_price_field` NAMES, whatever it is set to —
    # `build_feature_panel` raises "bars missing momentum price field" otherwise. The promoted config
    # uses `close`, which is already here, but the dataclass default is `close_split_dividend`, and a
    # constructor that only works for one of them is a landmine for the next config change.
    cols = ["ticker", "date", "open", "high", "low", "close", "volume"]
    field = getattr(cfg, "momentum_price_field", "close")
    if field not in cols:
        cols.append(field)
    empty = pd.DataFrame(columns=cols)
    if assets is None:
        # No reference data: fall back to the mode that does not require it, rather than raising.
        # This object is never consulted on the live path, and refusing to build it would take the
        # whole node down for a source nothing reads — which is exactly what just happened.
        from dataclasses import replace

        cfg = replace(cfg, asset_universe_mode="all")
    return QC345ComputedSource(empty, assets, cfg)
