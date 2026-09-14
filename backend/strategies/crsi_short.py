"""CRSISHORT-006 — the ConnorsRSI>90 / >100%-vol SHORT book, registered in SHADOW (#858).

WHAT THIS MODULE IS. The cockpit half of kumo-strategies#123: the builder that constructs the
adapter with everything cockpit owns (identity, universe, calendar, slots, lifecycle, journal,
budget) and the session gateway the adapter hands each session to. The strategy logic lives
upstream in `kumo_strategies.strategies.crsi_short`; nothing here decides anything.

WHAT IS DIFFERENT FROM THE LONG LANES, each a thing that was assumed everywhere else:

  side       The first lane that holds the SHORT side. `ownership.SHORT_PERMITTED` names it, and
             only it, so every other lane's short still alarms.
  prices     The adapter REFUSES to construct on anything but split-adjusted bars (a reverse split
             on raw prices books as a -1000% short loss on an open position). That is a fact about
             where the bars come from, so it is decided here from the feed's provider: IB DAY bars
             are `whatToShow=TRADES`, split-adjusted — measured on staging2's gateway, NVDA
             2024-06-07 close 120.89, the post-10:1 figure. Cockpit's Alpaca path is
             `adjustment=raw` by design with a tripwire, so on that feed this builder refuses and
             names #854 rather than constructing a lane that would be wrong on its first split.
  borrow     A locate fee ceiling with no locate data is an armed, inert gate (#26). The adapter
             demands a provider at construction. Three states here, never two: a provider from the
             feed (#857, IB tick 236 → LOCATABLE / None), an EXPLICIT `CRSI_BORROW_GATE_OFF`
             setting that turns the ceiling off and is recorded, or neither — which refuses and
             names #857.
  orders     The installed `broker.py` submits MARKET DAY orders and nothing else
             (kumo-strategies#131). CRSISHORT's entry is a sell-short LIMIT resting one session and
             #123 measured the market variant as LOSING above 100 bps of cost. So the gateway
             carries SHADOW only: it journals the full intent and submits nothing, and a lifecycle
             row moved to TRADING before #131 lands is REFUSED with a `risk` row, never quietly
             downgraded to a market order.

Registering is not arming. `CRSI_ENABLED` (settings, default false — no env gate by policy,
`strategy_registry.py:62-74`) registers the lane; the lifecycle row governs what it may do, and an
absent row reads as TRADING (Operator, 2026-08-19) — which is why the deploy that enables this lane
writes a SHADOW row in the same step (instances RUNBOOK).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
import math

# Module-level on purpose: `test_subscribe_what_we_hold` patches `<lane>._held_claims` and drives the
# real builder; a function-local import would make that patch a no-op and the test vacuous.
from strategies.qc27 import _held_claims  # noqa: E402

_log = logging.getLogger("kumo.strategies.crsi_short")

STRATEGY_ID = "CRSISHORT-006"
ORDER_ID_TAG = "006"

#: WHY TRADING IS REFUSED, stated as the fact it is: THIS gateway has no submission path. The
#: rotation lanes submit through a runner (pgrunner / QC27SessionRunner); CRSISHORT's runner is
#: this class, and it builds no order — the LOO-plus-DAY decomposition of one intent (the gap arm
#: fills at the opening print, the limit arm rests after the auction) is a decision-layer design
#: recorded on kumo-strategies#131 and not built here. The installed broker CAN carry a LIMIT and a
#: cancel since kumo-strategies 37569de; that is not the gate. Named in every refusal.
_NO_ORDER_PATH = ("CRSISHORT will not submit (cockpit#858): kumo-strategies ORDER_PATH_COMPLETE is "
                  "False — the entry rests in the OPENING CROSS (108 of 231 trades, 77.6% of the "
                  "return) and a day-limit-only lane is a different strategy at a quarter of the "
                  "edge (kumo-strategies#131)")

#: Sessions-to-calendar-days with slack for holidays and a boot mid-week. 252 sessions/365 days.
_CALENDAR_DAYS_PER_SESSION = 365.0 / 252.0
_HISTORY_SLACK_DAYS = 14


def _settings() -> dict:
    from api import settings

    return settings.resolve("strategies") or {}


def _enabled() -> bool:
    """From SETTINGS, not the environment — registering a strategy is an operator decision."""
    return bool(_settings().get("CRSI_ENABLED"))


def _universe_symbols() -> list[str]:
    from api.lane_universes import universe_symbols

    # ONE reader with the IB connector's `load_contracts` (#871).
    return universe_symbols(_settings(), "CRSI_UNIVERSE")


def _min_warm_symbols() -> int:
    """THE UNIVERSE BREADTH IS A DEPLOYMENT DECISION AND IT IS STATED (adapter docstring). The
    schema default is 400; the adapter refuses < 1."""
    return int(_settings().get("CRSI_MIN_WARM_SYMBOLS") or 400)


def _borrow_gate_off() -> bool:
    return bool(_settings().get("CRSI_BORROW_GATE_OFF"))


def _history_days(cfg) -> int:
    """Calendar days that cover the adapter's own warmup in sessions, with slack. Derived from the
    config, never a literal beside it — two numbers for one fact drift."""
    from kumo_strategies.runtime.nautilus.crsi_short import warmup_bars_needed

    sessions = warmup_bars_needed(cfg)
    return int(math.ceil(sessions * _CALENDAR_DAYS_PER_SESSION)) + _HISTORY_SLACK_DAYS


def _data_provider(feed) -> str | None:
    cfg = getattr(feed, "_cfg", None)
    provider = getattr(cfg, "data_provider", None)
    return str(provider).strip().lower() if provider else None


def _daily_bars_cover(feed) -> str | None:
    """What session the feed's DAILY bars aggregate over — the provider's own declaration
    (`DataClientSpec.daily_bars_cover`, #616), read off the feed. None = undeclared."""
    cover = getattr(feed, "_daily_bars_cover", None)
    return str(cover).strip().lower() if cover else None


def _accept_extended_daily_bars() -> bool:
    return bool(_settings().get("CRSI_ACCEPT_EXTENDED_DAILY_BARS"))


#: The lifecycle state registration WRITES when no row exists. An absent row reads as TRADING
#: (Operator, 2026-08-19); for a lane whose gateway refuses TRADING that is safe but it is not what
#: "registered in SHADOW" promises, so the row is created here and says who wrote it.
_REGISTRATION_STATE = "SHADOW"


class CrsiShortSessionGateway:
    """The session runner the adapter calls. SHADOW only, by construction — see the module docstring.

    `journal` is a PUBLIC attribute under that exact name: the adapter's `session_journal()`
    looks for it there to write the session's `state` row (kumo-cockpit#587 — a gateway that
    stored it as `_journal` wrote nothing for a lane's entire life). This gateway therefore writes
    `decision`, `risk` and `error` rows and leaves the outcome row to the adapter — one `state` row
    per session COROUTINE INVOCATION (a replay of an already-decided slot writes a second one; the
    decision itself is written once, keyed by the journal — codex, #858 review).
    """

    def __init__(self, *, journal, broker, read_state, intent, strategy_id: str = STRATEGY_ID,
                 read_budget=None, write_claim=None, cfg=None, slots=()):
        self.journal = journal
        self._broker = broker
        self._read_state = read_state
        #: EXECUTION DEPS. Absent -> this gateway still refuses to submit and says WHY, which is the
        #: behaviour it shipped with. Present -> it executes. Both states are reachable deliberately:
        #: a build that cannot read a budget must not send a short, and must not pretend it did.
        self._read_budget = read_budget
        self._write_claim = write_claim
        self._cfg = cfg
        #: The lane's decision ladder — a CALLABLE from the builder, or a tuple in a test.
        #: LAZY DELIBERATELY: resolving it eagerly at build time reads settings during
        #: construction, which warms `api.settings`' cache and made an unrelated test pass that
        #: asserts a raising store yields None. A build-time read is also wrong on its own terms —
        #: an operator can change the ladder after the node is up.
        self._slots = slots if callable(slots) else tuple(str(x) for x in (slots or ()))
        #: `(session: pd.Timestamp, panel) -> SessionIntent` — the adapter's own `session_intent`,
        #: bound after construction because the adapter needs this gateway to exist first.
        self.intent = intent
        self.strategy_id = strategy_id
        self._slot: str | None = None

    async def run(self, panel, session: str, jobs=None, slot: str | None = None):
        """`jobs` AND `slot` are the SessionRunner protocol and both default — a missing kwarg
        killed two lanes at the decision call (#831 shape). `jobs` is unused: this lane takes its
        universe as a settings array, not from the pool refresher."""
        import pandas as pd
        from kumo_strategies.runtime.executor.runner import SessionResult

        if slot:
            self._slot = slot
        life = await self._read_state()
        state = life.state

        def _result(**kw):
            return SessionResult(session=session, state=state.value, **kw)

        if not state.decides:
            return _result(decided=False, blocked=f"lifecycle {state.value}")

        if await self.journal.decided_this_session(session, self._slot):
            # The journal is the idempotency key (#189, #29): a restart mid-session must not
            # journal the same slot twice.
            return _result(decided=True, submitted=0, blocked="already decided this slot (resume)")

        try:
            intent = self.intent(pd.Timestamp(session), panel)
        except Exception as exc:                                       # noqa: BLE001
            await self.journal.write(
                "error", f"CRSISHORT intent failed: {type(exc).__name__}: {exc}",
                session=session, slot=self._slot)
            return _result(decided=False, blocked=f"intent failed: {type(exc).__name__}")

        acting = state.may_submit_entries or state.may_submit_exits
        if acting and not self._can_execute():
            # STILL REFUSED BEFORE ANY BROKER READ (codex, #858 review): the refusal must not depend
            # on a venue call succeeding, or a venue outage turns "refused, nothing sent" into "no row".
            why = f"lifecycle {state.value} refused: {_NO_ORDER_PATH} — nothing sent"
            await self.journal.write("risk", why, session=session, slot=self._slot,
                                     detail={"reason": _NO_ORDER_PATH, "intent": {
                                         "position_side": "SHORT", "enter": list(intent.enter),
                                         "limits": dict(intent.limits), "cover": dict(intent.cover),
                                         "refused": dict(intent.refused)}})
            return _result(decided=True, submitted=0, blocked=why)
        if acting:
            return await self._submit(intent, session, state, _result)
        try:
            held = sorted(sym for sym, qty in self._broker.strategy_positions().items() if qty)
        except Exception as exc:                                       # noqa: BLE001
            # SHADOW still journals what it wanted; the book read is named as unavailable rather
            # than rendered as "held nothing".
            await self.journal.write("error", f"CRSISHORT held-book read failed: "
                                     f"{type(exc).__name__}: {exc}", session=session, slot=self._slot)
            held = None
        detail = {
            "position_side": "SHORT",
            "enter": list(intent.enter), "limits": dict(intent.limits),
            "cover": dict(intent.cover), "cover_kind": dict(intent.cover_kind),
            "cover_px": dict(intent.cover_px), "refused": dict(intent.refused),
            "held": held, "shadow": True,
        }
        await self.journal.write(
            "decision",
            f"CRSISHORT {session}: enter {list(intent.enter)} · cover {list(intent.cover)} · "
            f"refused {len(intent.refused)} · held "
            f"{'unreadable' if held is None else len(held)} — SHADOW, nothing sent",
            session=session, slot=self._slot, detail=detail)
        return _result(decided=True, submitted=0, held=tuple(held or ()), detail=detail)
    def _can_execute(self) -> bool:
        """All three deps AND the upstream order path, or this lane does not submit.

        `ORDER_PATH_COMPLETE` IS THE AUTHORITY, READ LIVE FROM kumo-strategies — never mirrored here.
        The entry is a sell-short LIMIT resting in the OPENING CROSS: 108 of 231 accepted trades fill
        at the opening print and carry 77.6% of the return, +9.99%/trade against +2.53%
        (`crsi_short.py:89`). A lane sending only the day limit is a DIFFERENT STRATEGY at roughly a
        quarter of the measured edge, with every surface reading normal — so "trades, badly" is not
        an acceptable stopgap for "does not trade".

        THE FLAG THIS REPLACES WAS DEAD. `shadow_only` is assigned at `crsi_short.py:376` and READ
        NOWHERE — it gated a build-time raise and nothing at runtime. That was invisible while the
        gateway had no submission path, because a knob wired to nothing and a knob wired to something
        that never fires look identical. Wiring the path is exactly what makes it matter.
        """
        from kumo_strategies.runtime.nautilus.crsi_short import ORDER_PATH_COMPLETE

        if not ORDER_PATH_COMPLETE:
            return False
        return None not in (self._read_budget, self._write_claim, self._cfg)

    async def _row(self, kind: str, code: str, message: str, **kw):
        return await self.journal.write(kind, message, session=kw.pop("session", None),
                                        slot=self._slot, detail={"code": code, **kw})

    async def _replace_unfilled(self, intent, session: str, state, _result, sleeve, deployed):
        """After the cross: the auction leg's intent, resting as a day limit, for the legs it is OWED.

        WHY THE `client_order_id` IS READ BACK RATHER THAN RECOMPUTED. It is deterministic in
        (symbol, side, qty, session, strategy_id, slot, tif), so recomputing looks free — but `qty`
        came from the SLEEVE at the earlier slot and the sleeve can move between slots. A recomputed
        id that misses by one share is not an error, it is a DIFFERENT order that the cache will not
        find, and the lane would silently conclude every leg was filled. The journal row is
        PROVENANCE: what this lane actually sent, not what it would send now.

        `replacement_is_owed` IS UPSTREAM'S, and every answer it gives is the same question — can a
        second order double the position. ACCEPTED means still live IN the auction, so a replacement
        would fill ALONGSIDE it; PARTIALLY_FILLED means the slot is taken and topping up is a
        different decision this lane does not make; REJECTED means resting the same intent again
        converts a refusal into a silent retry.

        SAME PRICE, DELIBERATELY (upstream `replacement_leg`): the backtest models one limit for the
        whole session, so a re-derived price would be a different decision from the one measured.
        """
        from kumo_strategies.runtime.nautilus.crsi_short import replacement_is_owed
        from strategies.delta_execution import execute_plan, read_prices

        auction_slot = next((sl for sl in self._slots_today() if str(sl).startswith("open-")), None)
        if auction_slot is None:
            return _result(decided=True, submitted=0,
                           blocked="no pre-open slot configured — the auction leg was never placed")
        prior = await self.journal.explain(session, auction_slot)
        legs = list(((prior or {}).get("detail") or {}).get("sent") or [])
        legs = [x for x in legs if str(x.get("time_in_force")) == "AT_THE_OPEN"]
        if not legs:
            return _result(decided=True, submitted=0,
                           blocked=f"no auction legs recorded for {session}/{auction_slot}")

        status_of = getattr(self._broker, "order_status", None)
        if status_of is None:
            # NOT ASSUMED GONE. Without the venue's answer a replacement could double a live
            # position, which is the one outcome `replacement_is_owed` exists to prevent.
            await self._row("risk", "no_order_status", "broker cannot report order status — the "
                            "auction legs' fate is UNKNOWN and no replacement is sent",
                            session=session)
            return _result(decided=True, submitted=0, blocked="order status unavailable")

        try:
            book = {str(k): float(v) for k, v in dict(self._broker.strategy_positions()).items()}
        except Exception as exc:                                       # noqa: BLE001
            await self._row("error", "book_unreadable", f"CRSISHORT held-book read failed: "
                            f"{type(exc).__name__}: {exc}", session=session)
            return _result(decided=False, submitted=0, blocked="book_unreadable")

        owed, skipped = [], {}
        for leg in legs:
            coid, sym = str(leg.get("client_order_id")), str(leg.get("symbol"))
            st = status_of(coid)
            if st is None:
                skipped[sym] = "auction_leg_not_in_cache"
                continue
            if not replacement_is_owed(st):
                skipped[sym] = f"auction_leg_{str(st).lower()}"
                continue
            # THE BOOK IS A SECOND, INDEPENDENT DERIVATION OF "DID IT FILL?", AND IT ONLY EVER
            # VETOES. Two readings of one fact disagreeing is the detector; enabling on the weaker
            # one would be the opposite of that. A status saying the auction leg is gone while the
            # lane is already short that name means the fill reached the book and not the order
            # plane — and a replacement then DOUBLES a live short, which is the one outcome this
            # phase exists to prevent. Never the reverse: a flat book does NOT license a send when
            # the status is unknown, because a fill that has not propagated yet looks exactly like
            # no fill at all.
            if float(book.get(sym, 0.0) or 0.0) < 0.0:
                skipped[sym] = "already_short_despite_status"
                continue
            owed.append(_ShortOrder(symbol=sym, delta=float(leg.get("delta", 0.0)),
                                    target_qty=float(leg.get("target_qty", 0.0)),
                                    held_qty=float(leg.get("held_qty", 0.0)),
                                    reason="replacement for an unfilled auction leg"))
        for sym, code in sorted(skipped.items()):
            await self._row("risk", code, f"{sym}: {code} — no replacement", session=session,
                            symbol=sym)
        if not owed or not state.may_submit_entries:
            return _result(decided=True, submitted=0,
                           blocked=None if owed else "no auction leg is owed a replacement")

        prices = read_prices(self._broker, [o.symbol for o in owed])
        sent, refused, entered, exited = await execute_plan(
            owed, broker=self._broker, write_claim=self._write_claim, row=self._row, sleeve=sleeve,
            deployed=deployed, budget_code=None, prices=prices, session=session,
            slot=self._slot or "", strategy_id=self.strategy_id, position_side="SHORT",
            limits=dict(intent.limits or {}), time_in_force="DAY")
        detail = {"position_side": "SHORT", "phase": "replacement", "auction_slot": auction_slot,
                  "sent": sent, "refused": dict(refused) | skipped}
        row_id = await self.journal.write(
            "decision",
            f"CRSISHORT {session}: replacement for {[o.symbol for o in owed]} · sent {len(sent)} · "
            f"skipped {sorted(skipped)}",
            session=session, slot=self._slot, detail=detail)
        detail["decision_durable"] = row_id is not None
        return _result(decided=True, submitted=len(sent), entered=tuple(entered),
                       exited=tuple(exited), detail=detail)

    def _slots_today(self) -> tuple:
        """The slots this gateway was BUILT with — never a second read of settings.

        The adapter already resolves them once (settings over the built-in) and is handed the
        result; re-reading here would be a second derivation of the same fact, free to drift the
        moment an operator changes the ladder mid-session. The auction phase is decided from this
        list, so a drift would decide the wrong phase.
        """
        raw = self._slots() if callable(self._slots) else self._slots
        return tuple(str(x) for x in (raw or ()))

    async def _submit(self, intent, session: str, state, _result):
        """COVERS FIRST, THEN ENTRIES — and the order is load-bearing, not tidy.

        A cover RELEASES borrow and frees the sleeve room the next entry is gated against, exactly as
        a sell funds a buy on a long lane. `execute_plan` already withholds entries after a refused
        exit; sending entries first would make that protection unreachable.
        """
        from strategies.delta_execution import execute_plan, read_prices, read_sleeve

        sleeve, deployed, budget_code = await read_sleeve(self._read_budget, self._row, session=session)
        if budget_code is not None:
            why = f"{budget_code}: the sleeve is the sizing basis and it could not be read — nothing sent"
            return _result(decided=False, submitted=0, blocked=why)

        try:
            book = {str(k): float(v) for k, v in dict(self._broker.strategy_positions()).items()}
        except Exception as exc:                                       # noqa: BLE001
            # NOT rendered as "held nothing": an unreadable book would size covers as if flat and
            # could leave a short open with nothing chasing it.
            await self._row("error", "book_unreadable", f"CRSISHORT held-book read failed: "
                            f"{type(exc).__name__}: {exc}", session=session)
            return _result(decided=False, submitted=0, blocked="book_unreadable")

        # PHASE, FROM THE SLOT'S OWN NAME. A slot resolving BEFORE the open is the only place an
        # order can rest in the OPENING CROSS, and that is where 108 of 231 accepted trades fill and
        # 77.6% of the return comes from (kumo-strategies `crsi_short.py:89`). Reading the phase
        # from the name is a rule; a list of which slots are auction slots would be a second thing
        # to keep correct.
        auction_phase = str(self._slot or "").startswith("open-")
        if not auction_phase:
            return await self._replace_unfilled(intent, session, state, _result, sleeve, deployed)

        covers, cover_refused = _cover_orders(intent, book)
        basis = min(float(sleeve.actual), float(sleeve.target)) if sleeve is not None else 0.0
        entries, entry_refused = _size_entries(intent, book, basis, self._cfg)
        orders = covers + (entries if state.may_submit_entries else [])
        skipped = dict(cover_refused) | dict(entry_refused)
        if entries and not state.may_submit_entries:
            skipped |= {o.symbol: "entries_not_permitted" for o in entries}
        for sym, code in sorted(skipped.items()):
            await self._row("risk", code, f"{sym}: {code} — not sent", session=session, symbol=sym)

        prices = read_prices(self._broker, [o.symbol for o in orders])
        common = dict(broker=self._broker, write_claim=self._write_claim, row=self._row,
                      sleeve=sleeve, deployed=deployed, budget_code=budget_code, prices=prices,
                      session=session, slot=self._slot or "", strategy_id=self.strategy_id,
                      position_side="SHORT")
        # COVERS AT MARKET, DELIBERATELY. `cover_px` is the price the RULE meant and it is journalled
        # as such; the driver owns slippage and fees (`SessionIntent`'s own docstring). A cover that
        # rests on a limit and does not fill leaves a short OPEN against a rule that said close it —
        # the one asymmetry with an entry, where not filling simply means no position.
        sent, refused, entered, exited = await execute_plan(
            [o for o in orders if o.delta > 0], limits=None, time_in_force="DAY", **common)
        # ENTRIES INTO THE CROSS. `AT_THE_OPEN` maps to Nautilus' TimeInForce.AT_THE_OPEN, which the
        # IB adapter sends as OPG; a silent downgrade to DAY forgoes three quarters of the lane's
        # return without failing anywhere (kumo-strategies `crsi_short.py:115`).
        e_sent, e_refused, e_entered, e_exited = await execute_plan(
            [o for o in orders if o.delta < 0], limits=dict(intent.limits or {}),
            time_in_force="AT_THE_OPEN", **common)
        sent, entered, exited = sent + e_sent, entered + e_entered, exited + e_exited
        refused = dict(refused) | dict(e_refused)

        refused = dict(refused) | skipped
        detail = {"position_side": "SHORT", "enter": list(intent.enter),
                  "limits": dict(intent.limits), "cover": dict(intent.cover),
                  "cover_kind": dict(intent.cover_kind), "cover_px": dict(intent.cover_px),
                  "refused": dict(intent.refused) | refused, "held": sorted(book),
                  "sent": sent, "shadow": False,
                  "sized": {o.symbol: float(o.delta) for o in orders}}
        partial = bool(refused) and bool(orders)
        row_id = await self.journal.write(
            "decision",
            f"CRSISHORT {session}: short {[o.symbol for o in orders if o.delta < 0]} · "
            f"cover {[o.symbol for o in orders if o.delta > 0]} · sent {len(sent)} · "
            f"refused {len(refused)} — {'PARTIAL' if partial else state.value}",
            session=session, slot=self._slot, detail=detail)
        detail["decision_durable"] = row_id is not None
        blocked = None
        if partial:
            blocked = (f"PARTIAL: sent {[x['symbol'] for x in sent]}, refused {sorted(refused)} — "
                       f"the plan was not completed this session")
        elif row_id is None:
            blocked = f"decision row NOT durable (journal returned None) after {len(sent)} order(s)"
        return _result(decided=True, submitted=len(sent), entered=tuple(entered), exited=tuple(exited),
                       held=tuple(sorted(book)), detail=detail, blocked=blocked)




@dataclass(frozen=True)
class _ShortOrder:
    """What `delta_execution` consumes. Signed for a SHORT book, which is the whole subtlety.

    `held_qty` is NEGATIVE while short, `delta` is negative to open and positive to cover, and
    `post_trade_qty` is their sum — so a claim written from it is signed, which is exactly what
    `CLAIMS_REPRESENT_SHORT` (kumo-strategies#173) means. Pinned against the seam's real contract by
    `test_the_short_order_satisfies_the_execute_plan_contract`.
    """

    symbol: str
    delta: float
    target_qty: float
    held_qty: float
    reason: str

    @property
    def post_trade_qty(self) -> float:
        return self.held_qty + self.delta


def _size_entries(intent, held: dict, basis: float, cfg) -> tuple[list, dict[str, str]]:
    """Entries sized through `cfg.slot_notional`, THE BACKTEST'S OWN DERIVATION.

    `runner_crsi_short.py:243` computes `-int(cfg.slot_notional(equity) / fill)`. Live re-deriving
    that arithmetic is the divergence shape this repo keeps paying for — the give-back rule, the ATR
    rules and the highs all split that way. So this calls the same method and rounds the same
    direction (`int`, toward zero, never up into a bigger short than the sleeve funded).

    THE BASIS IS THE SLEEVE, NOT THE ACCOUNT. QC345-003 sized off a 103,466 account against a 20,000
    sleeve and could never fit a single entry; TECHIVOL-005 had the same defect from a hardcoded
    constant. `min(actual, target)` is what the lane physically has AND is allowed to use.

    A name with no limit price is REFUSED BY NAME rather than sized off a fallback: the limit is the
    price the rule chose, and substituting any other price makes this a different strategy.
    """
    orders: list[_ShortOrder] = []
    refused: dict[str, str] = {}
    per_name = float(cfg.slot_notional(basis))
    for sym in intent.enter:
        px = (intent.limits or {}).get(sym)
        if px is None or not (float(px) > 0.0):
            refused[sym] = "no_entry_limit"
            continue
        qty = int(per_name / float(px))
        if qty < 1:
            refused[sym] = "slot_below_one_share"
            continue
        held_qty = float(held.get(sym, 0.0) or 0.0)
        orders.append(_ShortOrder(symbol=sym, delta=-float(qty), target_qty=held_qty - float(qty),
                                  held_qty=held_qty, reason="short entry"))
    return orders, refused


def _cover_orders(intent, held: dict) -> tuple[list, dict[str, str]]:
    """Covers close what THIS LANE's book says it is short — never a target, never a fraction.

    A cover for a name the lane is not short is dropped with a code rather than sent: buying a name
    we do not owe turns a bookkeeping disagreement into a LONG position, which is the one outcome a
    short lane must never produce by accident.
    """
    orders: list[_ShortOrder] = []
    refused: dict[str, str] = {}
    for sym in sorted(intent.cover):
        held_qty = float(held.get(sym, 0.0) or 0.0)
        if held_qty >= 0.0:
            refused[sym] = "cover_without_a_short"
            continue
        orders.append(_ShortOrder(symbol=sym, delta=-held_qty, target_qty=0.0, held_qty=held_qty,
                                  reason=str((intent.cover_kind or {}).get(sym, "cover"))))
    return orders, refused


async def _ensure_registration_row(sm) -> str:
    """Registration writes the SHADOW row when none exists (codex, #858 review). An explicit row
    always wins — an operator's DISABLED/HALTED/TRADING is never overwritten. Returns the state
    the lane will read at its first session."""
    from kumo_strategies.runtime.executor.store import StrategyState, select

    async with sm() as s:
        row = (await s.execute(select(StrategyState).where(
            StrategyState.strategy_id == STRATEGY_ID))).scalars().first()
        if row is not None:
            return str(row.state)
        s.add(StrategyState(
            strategy_id=STRATEGY_ID, state=_REGISTRATION_STATE,
            reason="registration: CRSISHORT-006 starts in SHADOW (cockpit#858) — computes and "
                   "journals its intent, submits nothing; an operator moves it"))
        await s.commit()
    _log.info("%s: no lifecycle row — wrote %s at registration", STRATEGY_ID, _REGISTRATION_STATE)
    return _REGISTRATION_STATE


def build_crsi_short_strategy(*, feed=None):
    """CRSISHORT-006 wired to `CrsiShortSessionGateway`, or None when the gate is off.

    RAISES when the gate is ON but a prerequisite is missing, rather than returning None — a lane
    that is switched on and silently absent is the worst outcome available. `build_optional_strategy`
    in `engine_node` separates that from transient transport failure by exception TYPE.
    """
    if not _enabled():
        return None

    symbols = _universe_symbols()
    if not symbols:
        raise RuntimeError(
            "strategies.CRSI_ENABLED is on but `strategies.CRSI_UNIVERSE` is empty — refusing to "
            "register a strategy that can never decide. Set the universe in settings, or unset the "
            "gate.")

    import asyncio
    from dataclasses import replace

    from kumo_strategies.runtime.calendar import build_calendar
    from kumo_strategies.runtime.executor.pgjournal import PgJournal
    from kumo_strategies.runtime.executor.store import create_all, make_engine, make_sessionmaker
    from kumo_strategies.runtime.nautilus.broker import NautilusBroker

    async def _prepare():
        eng = make_engine(None, cross_loop=True)
        await create_all(eng)
        sm = make_sessionmaker(eng)
        held = await _held_claims(sm, STRATEGY_ID)
        try:
            await _ensure_registration_row(sm)
        except Exception as exc:                                       # noqa: BLE001
            # BEST EFFORT, like `_held_claims`: a build failure leaves the node running with one lane
            # fewer and no obvious cause. Without the row the lane reads TRADING and the gateway
            # refuses it — safe, and said here.
            _log.error("%s: could not write the registration lifecycle row (%r) — the lane reads "
                       "TRADING (absent row) and the gateway refuses it", STRATEGY_ID, exc)
        return sm, held

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        sm, held = asyncio.run(_prepare())
        symbols = sorted(set(symbols) | held)
    else:  # pragma: no cover — build runs before the node's loop exists
        raise RuntimeError(
            "build_crsi_short_strategy must run before the node's event loop starts — it opens a "
            "cross-loop engine for the lifecycle and journal stores")

    # THE LAST POINT AT WHICH THE UNIVERSE IS STILL JUST NAMES — the same seam every other builder
    # passes through, so `test_subscribe_what_we_hold` drives this one too (#622).
    from strategies.momentum import _lane_symbols

    symbols = _lane_symbols(symbols)

    # THE ADAPTER IS A PIN QUESTION, answered here by name. The production pin may deliberately
    # predate kumo-strategies#121 (Operator, 2026-09-11: not now); with the gate ON and no adapter the
    # honest outcome is a boot failure that says which revision is missing — never a bare
    # ModuleNotFoundError from a nested import, and never a lane that silently does not register.
    try:
        from kumo_strategies.runtime.nautilus.crsi_short import CrsiShortStrategy
        from kumo_strategies.strategies.crsi_short import ADJUSTED, CrsiShortConfig
    except ImportError as exc:
        raise RuntimeError(
            f"strategies.CRSI_ENABLED is on but the installed kumo-strategies has no CRSISHORT "
            f"adapter ({exc}) — the pin predates kumo-strategies#121. Unset the gate or move the "
            f"pin; the lane cannot register on this revision.") from exc

    # BARS: split-adjusted AND regular-trading-hours, both decided by where they come from. IB's
    # TRADES daily bars are split-adjusted (measured, NVDA 2024-06-07 = 120.89); cockpit's Alpaca
    # path is `adjustment=raw` with a tripwire (#854). The IBKR data client declares
    # `daily_bars_cover="extended"` (pre/post-market folded into the daily OHLCV, #616) while the
    # lab's panel is RTH SIP daily — prior close, high, low and volume all differ. Refused unless the
    # deviation is STATED in settings, and then said at WARNING on every boot.
    provider = _data_provider(feed)
    if provider != "ibkr":
        raise RuntimeError(
            f"strategies.CRSI_ENABLED is on but this node's bars come from {provider!r} — CRSISHORT "
            f"requires SPLIT-ADJUSTED daily bars, which IB's TRADES bars are (measured) and "
            f"cockpit's Alpaca path (`adjustment=raw`, tripwired) is not. See kumo-cockpit#854 "
            f"before enabling this lane on a non-IBKR instance.")
    # BORROW: three states, never two. A provider keeps the fee ceiling ON (it answers LOCATABLE or
    # None per name); an explicit gate-off turns the ceiling off and is recorded; neither refuses.
    cfg = CrsiShortConfig()
    borrow_rates = getattr(feed, "shortable_provider", None)
    if borrow_rates is None:
        if not _borrow_gate_off():
            raise RuntimeError(
                "strategies.CRSI_ENABLED is on but the node has no shortable provider "
                "(kumo-cockpit#857) and `strategies.CRSI_BORROW_GATE_OFF` is not set — a locate "
                "ceiling with no locate data is armed and inert. Wire #857, or set the gate off "
                "explicitly and record it in the instance RUNBOOK.")
        _log.warning("%s: borrow gate OFF by `CRSI_BORROW_GATE_OFF` — no locate data on this node; "
                     "entries are not screened for availability", STRATEGY_ID)
        cfg = replace(cfg, max_borrow_fee_annual=None)
    cover = _daily_bars_cover(feed)
    if cover != "rth":
        if not _accept_extended_daily_bars():
            raise RuntimeError(
                f"strategies.CRSI_ENABLED is on but this node's daily bars cover {cover!r} — CRSISHORT "
                f"was measured on REGULAR-HOURS daily bars, and extended-hours OHLCV changes prior "
                f"close, high/low and volume the screen and the exits read. Either give the lane RTH "
                f"daily bars (per-request use_rth on the IB data client) or set "
                f"`strategies.CRSI_ACCEPT_EXTENDED_DAILY_BARS` and record the deviation.")
        _log.warning("%s: running on %r daily bars by `CRSI_ACCEPT_EXTENDED_DAILY_BARS` — a stated "
                     "deviation from the measured strategy (RTH)", STRATEGY_ID, cover)
    elif _accept_extended_daily_bars():
        # AN ACCEPTED DEVIATION THAT IS NOT OCCURRING IS A CONTRADICTION (l21wvpmj, #951 review).
        # Left set on an RTH node (#875) the setting is never read, changes nothing, and survives —
        # until a revert or a deploy to an extended node applies it without anyone re-deciding.
        # Refusing here is what stops "a human stated this on this date" rotting into "a value
        # nobody remembers setting".
        raise RuntimeError(
            f"strategies.CRSI_ACCEPT_EXTENDED_DAILY_BARS is set but this node's daily bars cover "
            f"{cover!r} — the accepted deviation is not occurring. Unset it; an acceptance must "
            f"describe a deviation that exists, or it will apply silently the day one does.")

    async def _read_state():
        """A FRESH lifecycle read per session. AN ABSENT ROW IS TRADING (Operator, 2026-08-19) — the
        same rule every gateway applies; for THIS lane TRADING is then refused by the gateway
        while #131 is unmerged, so the absent-row default cannot send an order here."""
        from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State
        from kumo_strategies.runtime.executor.store import StrategyState, select

        async with sm() as s:
            row = (await s.execute(select(StrategyState).where(
                StrategyState.strategy_id == STRATEGY_ID))).scalars().first()
        return (Lifecycle(State(row.state), row.reason) if row
                else Lifecycle(State.TRADING, "default: no lifecycle row"))

    from strategies.decision_slots import BUILTIN_SLOTS
    from strategies.momentum import _read_slots_for, decision_slots_from_settings
    from strategies.slot_reader import _live_reread_kwargs

    broker = NautilusBroker(strategy=None, instrument_ids=None)   # resolved by the strategy (#622)
    journal = PgJournal(sm, strategy_id=STRATEGY_ID)

    async def _read_budget():
        """(sleeve, deployed) for a SHORT book. `deployed` is what this lane's claims say it is
        short, MARKED TO THE CURRENT PRICE — `abs()` because a short claim is signed negative
        (kumo-strategies#173) and capital at risk is a magnitude. A missing price RAISES rather
        than falling back to the entry price: that fallback is the #985 defect one level down."""
        from api.budget_store import load_book
        from kumo_strategies.runtime.executor.store import PositionState, select
        from strategies.delta_execution import read_prices as _read_prices
        async with sm() as s:
            book = await load_book(s)
            rows = (await s.execute(select(PositionState).where(
                PositionState.strategy_id == STRATEGY_ID))).scalars().all()
        held = {str(r.symbol): abs(float(r.qty)) for r in rows if r.qty}
        prices = _read_prices(broker, set(held))
        missing = sorted(k for k in held if prices.get(k) is None)
        if missing:
            raise RuntimeError(f"no price for held {missing} — the sleeve's deployed figure would be "
                               f"wrong and every entry gated against it")
        deployed = sum(qty * float(prices[sym]) for sym, qty in held.items())
        return book.sleeves.get(STRATEGY_ID), float(deployed)

    async def _write_claim(symbol: str, post_trade_qty: float, px):
        """SET to the post-trade quantity, SIGNED — negative while short. `CLAIMS_REPRESENT_SHORT`
        is what makes that legible to every other reader (kumo-strategies#173)."""
        from api.claims_sync import sync_claim_from_book
        await sync_claim_from_book(journal, STRATEGY_ID, symbol, int(post_trade_qty), px)

    gateway = CrsiShortSessionGateway(
        journal=journal, broker=broker, read_state=_read_state, intent=None,
        read_budget=_read_budget, write_claim=_write_claim, cfg=cfg,
        slots=lambda: _read_slots_for(STRATEGY_ID, BUILTIN_SLOTS[STRATEGY_ID])
                      or BUILTIN_SLOTS[STRATEGY_ID])
    strategy = CrsiShortStrategy(
        cfg,
        symbols=symbols,
        order_id_tag=ORDER_ID_TAG,
        calendar=build_calendar(
            require_exchange=True,
            calendar=getattr(feed, "_venue_calendar", None),
        ),
        session_runner=gateway,
        history_days=_history_days(cfg),
        price_adjustment=ADJUSTED,
        min_warm_symbols=_min_warm_symbols(),
        borrow_rates=borrow_rates,
        # THE RESOLVED LADDER, NOT THE BUILT-IN (#131). kumo-strategies refuses at CONSTRUCTION when
        # a trading lane's slots cannot reach the auction, and it checks THIS argument. The built-in
        # is `("open+5m",)` — after the cross — so passing it with shadow_only=False raises at build
        # and, under #377, takes every other lane on the node down with it. The settings override is
        # what carries `open-10m`, and the constructor has to see it.
        decision_slots=decision_slots_from_settings(STRATEGY_ID, BUILTIN_SLOTS[STRATEGY_ID]),
        **_live_reread_kwargs(
            CrsiShortStrategy,
            read_slots=lambda: _read_slots_for(STRATEGY_ID, BUILTIN_SLOTS[STRATEGY_ID]),
            # SHADOW BY DECLARATION (kumo-strategies#141): the adapter refuses to construct a lane whose
            # order path is not built unless the caller says it will only compute and publish. That is
            # exactly this gateway's contract, so it is said at the one seam where the adapter asks.
            # Dropped by the same guard on a revision that predates the kwarg.
            # THE PATH IS BUILT, SO THIS DEPLOYMENT TRADES (kumo-strategies#131 landed at 7fae46b).
            # `shadow_only` is now LIVE upstream — it gates the submission handover, not just
            # construction — so leaving it True would register a lane that computes and publishes
            # and never sends, which is exactly what it said and nobody could observe while the
            # gateway had no submission path at all.
            #
            # SETTINGS-DRIVEN so a shadow deployment stays reachable WITHOUT a rebuild: that is
            # cockpit#853's first phase and the safest way to validate a new lane against a venue.
            # Default False because the lane's actual trading gates are `CRSI_ENABLED` and its
            # LIFECYCLE row, both of which default to not-trading; a third flag defaulting to
            # not-trading would just be a third place to forget.
            shadow_only=bool(_settings().get("CRSI_SHADOW_ONLY", False))),
    )
    # The gateway asks the adapter for WHAT it wants each session; the adapter has to exist first.
    gateway.intent = strategy.session_intent

    from api.venue_preference import prefer_primary_exchange

    strategy._prefer_venue = lambda sym, cands, _s=strategy: prefer_primary_exchange(_s.cache, sym, cands)
    broker.strategy = strategy
    broker.feed = feed
    return strategy
