"""TradeCycleProjection (#73) — the thin threading layer over NATIVE Nautilus state.

A trade CYCLE = open→closed spanning intra-cycle flats. It is NOT a parallel ledger: every number is derived
from native positions + position snapshots + working orders. There is no native Nautilus cycle primitive
(confirmed via codex + Perplexity), so this projection layers cycle identity + state on top.

It is an event FOLD, not a stateless cache scan: a scan can render current state but cannot recover whether a
past flat gap was bridged by a live entry order (same cycle) or the cycle had CLOSED (next open = new cycle).
So we maintain an in-memory active-cycle map keyed by the ADR canonical tuple
`(account_id, client_id, instrument_id, strategy_id)`; `project()` is called on position/order events and the
snapshot cadence, and reconciles the map. In-memory is v0 — durable/cold-restart reconstruction is #74. See
docs/plan-73-trade-cycle-projection.md and docs/adr/0001.

cycle_id is OPAQUE: `{account}:{client}:{instrument}:{strategy}:{open_anchor_ts_ns}`. Never parse it (the ids
carry `.`/`-` already). It is NOT the native position_id (a strategy key, reused across cycles) and NOT a
Nautilus TradeId (a fill id).

v0 limitations (accepted; codex-reviewed):
- A CLOSED cycle is emitted ONCE then dropped from the fold; the plane is a latest-state Redis key, so a
  consumer that isn't polling at that instant may not observe the terminal CLOSED. "Just closed" is ephemeral
  display in v0 — durable closed-cycle history is the trade ledger (#78) / restart durability (#74).
- The open anchor is a ns timestamp. Two DISTINCT cycles for the same key opening at the identical ns would
  collide on cycle_id (design allows a same-ts ordinal, not implemented) — impossible in practice, since a
  same-key reopen is separated from the prior close by real fills at distinct ns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.model.currencies import USD
from nautilus_trader.model.identifiers import ClientId, PositionId, StrategyId
from nautilus_trader.model.objects import Money
from nautilus_trader.model.position import Position

from api.models import TradeDTO, WorkingOrderDTO

_log = logging.getLogger(__name__)

# The identity key the fold tracks a live cycle under.
_Key = tuple[str, str, str, str]  # (account_id, client_id, instrument_id, strategy_id)


@dataclass
class _ActiveCycle:
    """The fold state for one live cycle — the minimum needed to keep cycle_id + boundaries stable across ticks.
    Everything else (P&L, side, legs) is re-derived from native state on each emit."""

    cycle_id: str
    opened_ts: int  # first-open ns — the anchor the cycle_id was minted from; STABLE across legs
    last_event_ts: int = 0  # monotonic fold clock — strictly advances per emit (durable-write ordering guard)


def newer_close_wins(prior_ts_closed: int | None, ts_closed: int | None) -> bool:
    """THE ONE RULE for two states of the same leg (#846): the later close replaces the earlier — a
    reconciliation re-apply rewrites a closed state. Three sites (the snapshot reader, the engine's
    registry, the realized fold) call this rather than each carrying a comparison that can drift."""
    return prior_ts_closed is None or (ts_closed or 0) >= prior_ts_closed


@dataclass(frozen=True)
class CycleLeg:
    """One closed leg of a cycle, reduced to what cycle P&L needs. The projection consumes these so a leg can
    come from EITHER a live Nautilus `Position` (in-memory `cache.position_snapshots`) OR a persisted position
    state-dict — the #74 restart gap-fill. `realized_pnl` is native (Nautilus computed it); this is not a
    parallel ledger, just a common shape over the two sources."""

    ts_opened: int
    ts_closed: int | None
    realized_pnl: Money
    #: WHOSE leg, on WHAT, under WHICH position id (#846). Cycle P&L never needed these, so the leg dropped
    #: them and a restored leg could not be attributed to a strategy. All three are in `Position.to_dict()`,
    #: on `Position`, and on `PositionClosed`. Defaulted so the positional constructor above is unchanged.
    strategy_id: str = ""
    instrument_id: str = ""
    position_id: str = ""

    @classmethod
    def from_position(cls, pos: Position) -> CycleLeg:
        return cls(pos.ts_opened, pos.ts_closed, pos.realized_pnl,
                   str(pos.strategy_id), str(pos.instrument_id), str(pos.id))

    @classmethod
    def from_event(cls, event) -> CycleLeg:
        """From a `PositionClosed` (#846) — the engine's own hook for a leg closing in THIS process.

        READ THE EVENT, NEVER THE CACHE. Nautilus queues position events and flushes them at the end of
        the fill that caused them (`ExecutionEngine`, `_pending_position_events`), after the cache is
        updated — on a flip the strategy sees the close with the reopened position already in the cache.
        The event carries `realized_pnl` and both timestamps as they were at the close."""
        return cls(int(event.ts_opened), int(event.ts_closed), event.realized_pnl,
                   str(event.strategy_id), str(event.instrument_id), str(event.position_id))

    @classmethod
    def from_state_dict(cls, d: dict, *, position_id: str | None = None) -> CycleLeg:
        """From Nautilus `Position.to_dict()` as persisted by `snapshot_position_state` to
        `snapshots:positions:{pos_id}` (the durable source Nautilus writes but never auto-reloads).

        Robust to non-closed states: `snapshot_positions=True` also persists opened/changed states, whose
        `realized_pnl` serializes as the string "None" (→ 0). The reader should still filter to CLOSED legs
        (`ts_closed is not None`) — see `is_closed_state` — but this never raises on an open/changed state."""
        rp = d.get("realized_pnl")
        ts_closed = d.get("ts_closed")
        return cls(
            ts_opened=int(d["ts_opened"]),
            ts_closed=int(ts_closed) if ts_closed is not None else None,
            realized_pnl=Money.from_str(rp) if rp and rp != "None" else Money(0, USD),
            strategy_id=str(d.get("strategy_id") or ""),
            instrument_id=str(d.get("instrument_id") or ""),
            # The Redis KEY is where Nautilus filed the state; it wins over the dict when the caller has it.
            position_id=str(position_id or d.get("position_id") or ""),
        )

    @staticmethod
    def is_closed_state(d: dict) -> bool:
        """Whether a persisted position state-dict is a CLOSED leg — the only kind the gap-fill should restore.
        The Redis reader (74b) filters on this so opened/changed snapshots aren't mistaken for legs."""
        return d.get("ts_closed") is not None


#: Order types that PROTECT. Mirrors `books.ts`'s own PROTECTIVE set and `protection.py`'s
#: PROTECTIVE_TYPES — a take-profit LIMIT reduces the position but sits above the market and does nothing
#: on the way down, so it is not protection here either.
_PROTECTIVE_ORDER_TYPES = frozenset({
    "STOP_MARKET", "STOP_LIMIT", "TRAILING_STOP_MARKET", "TRAILING_STOP", "STOP",
})


def _dedupe_orders(orders: list) -> list:
    """Order list deduped by client order id, first occurrence winning.

    This strategy's own protective orders appear in both inputs, and a duplicate would be counted twice by
    anything summing protected quantity.
    """
    seen: set[str] = set()
    out: list = []
    for o in orders:
        key = str(getattr(getattr(o, "client_order_id", None), "value", "") or id(o))
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out


class TradeCycleProjection:
    """Owns the active-cycle map for ONE strategy (v0 = MANUAL). Fed by `UiFeedStrategy` on position/order
    events and the snapshot cadence."""

    def __init__(self, client_id: ClientId, strategy_id: StrategyId) -> None:
        self._client_id = str(client_id)  # the EXECUTION client (not the data client)
        self._strategy_id = strategy_id
        self._strategy_str = str(strategy_id)
        self._account_id = ""  # resolved lazily from the native account on first project()
        self._account_warned = False
        self._active: dict[_Key, _ActiveCycle] = {}
        # #74 gap-fill: closed legs recovered from Nautilus's persisted snapshots on restart, keyed by the
        # position_id string. cache.position_snapshots() (in-memory) does NOT rehydrate, so without this cycle
        # P&L would lose every pre-restart leg. Merged with live snapshots in _build_dto.
        self._restored: dict[str, list[CycleLeg]] = {}

    def seed_restored_legs(self, pos_id: str, legs: list[CycleLeg]) -> None:
        """Restart seed (#74): register the closed legs recovered from persisted `snapshots:positions` for a
        position_id, so post-restart cycle P&L includes them. Call before the first project() after restart."""
        if legs:
            self._restored[pos_id] = list(legs)

    def seed_cycle(
        self, account_id: str, instrument_id: str, cycle_id: str, opened_ts: int, last_event_ts: int = 0
    ) -> None:
        """Restart seed (#74): restore one active cycle's identity + open anchor from the durable envelope, so
        the SAME reducer resumes on the SAME cycle_id (not a fresh mint) and its restored legs fall inside the
        ts_opened window. The envelope is the only place the pre-restart cycle BOUNDARY survives — native state
        can't tell whether a flat was bridged. `last_event_ts` restores the monotonic fold clock so post-restart
        writes keep out-ordering the pre-restart ones. `client_id`/`strategy_id` are the projection's own (it
        owns one strategy under one exec client)."""
        self._account_id = account_id  # the envelope's canonical account — keep project()'s _key consistent
        key = (account_id, self._client_id, instrument_id, self._strategy_str)
        self._active[key] = _ActiveCycle(cycle_id=cycle_id, opened_ts=opened_ts, last_event_ts=last_event_ts)

    def _resolve_account(self, cache) -> str:
        """The account is native (one exec account in v0) — read it from the cache rather than threading a
        value that could drift from the broker's real AccountId. Cached once resolved. If a restart seed set a
        different account than the live cache reports, keep the seeded (envelope) account but WARN — a silent
        account switch would file current positions under a stale identity (codex-flagged)."""
        accounts = cache.accounts()
        live = str(accounts[0].id) if accounts else ""
        if not self._account_id:
            self._account_id = live
        elif live and live != self._account_id and not self._account_warned:
            _log.warning(
                "seeded account %s != live cache account %s — keeping seeded (envelope) account",
                self._account_id,
                live,
            )
            self._account_warned = True
        return self._account_id

    def _key(self, instrument_id: str) -> _Key:
        return (self._account_id, self._client_id, instrument_id, self._strategy_str)

    def _instruments_in_play(self, cache) -> set[str]:
        """Every instrument this strategy currently has a native footprint on (open position, working order) plus
        any it is already tracking (so a cycle that just went flat still emits its terminal CLOSED)."""
        out: set[str] = {k[2] for k in self._active}
        for pos in cache.positions_open():
            if str(pos.strategy_id) == self._strategy_str:
                out.add(str(pos.instrument_id))
        for order in cache.orders_open():
            if str(order.strategy_id) != self._strategy_str:
                continue
            # A PROTECTIVE stop is not a claim on the position — it says "these shares have cover", not
            # "this strategy has a cycle here". An ENTRY order is what opens one.
            #
            # The #239 backstop submits through the display strategy, so every PROT- stop carries
            # MANUAL-001 even when it protects a MOMENTUM-002 position. Counting those as in-play minted
            # phantom ARMED cycles with qty 0 on seven MOMENTUM holdings, each rendering as its own
            # portfolio row reading "flat · 0 held · 1 armed" and claiming a manager that did not exist.
            if str(getattr(getattr(order, "order_type", None), "name", "")) in _PROTECTIVE_ORDER_TYPES:
                continue
            out.add(str(order.instrument_id))
        return out

    def _working_orders(self, cache, instrument_id: str) -> list:
        """THIS strategy's open orders on the instrument. Strategy-scoped deliberately — cycle lifecycle
        reads it to decide whether an entry order bridges a flat, and widening that would let one sleeve's
        buy keep another sleeve's cycle alive."""
        return [
            o
            for o in cache.orders_open()
            if str(o.strategy_id) == self._strategy_str and str(o.instrument_id) == instrument_id
        ]

    def _protective_orders_on_instrument(self, cache, instrument_id: str) -> list:
        """Reduce-only orders resting on the instrument, from ANY strategy (#289).

        Coverage is not an attribution question. A stop resting at the venue protects those shares whoever
        claims them — the broker does not know about our sleeves, which is the same rule `protection.py`
        states for the backstop's own audit.

        Observed on the paper book 2026-08-14: the BOOK tile reported 5 of 8 positions unprotected while 6
        of 8 had a working trailing stop. Every #239 backstop order carries MANUAL-001, because the
        reconciler places them through the display strategy, so all three MOMENTUM-002 positions read as
        naked. On a SAFETY display, answering "is this covered" with "did I place it" is the dangerous
        direction of wrong.

        Matched on ORDER TYPE, not on `is_reduce_only`. The #239 backstop does not set that flag, so a
        reduce-only filter adopted nothing it placed — BDX, VCTR and WHD kept reading naked with working
        stops resting on them, and the unit test passed only because its double set the flag production
        does not. A stop on the reducing side IS protection whatever the flag says; an entry order is not
        a stop, so the type check excludes it just as surely.
        """
        return [
            o
            for o in cache.orders_open()
            if str(o.instrument_id) == instrument_id
            and str(getattr(getattr(o, "order_type", None), "name", "")) in _PROTECTIVE_ORDER_TYPES
        ]

    def project(self, cache, now_ns: int = 0) -> list[TradeDTO]:
        """Reconcile the fold against current native state; return one TradeDTO per live-or-just-closed cycle.
        A cycle that reaches CLOSED is emitted once (terminal) and then dropped from the map — the next open on
        that instrument mints a fresh cycle_id. `now_ns` is the engine clock at this event — it stamps each
        cycle's monotonic fold clock so a terminal CLOSED (esp. a no-fill ARMED cancel, which carries no native
        timestamp) always out-orders the prior write in the durable envelope."""
        if not self._resolve_account(cache):
            # No native account yet → do NOT mint: a cycle keyed on account_id="" would be re-minted under a
            # new key (and a new cycle_id) the moment the account resolves, stranding the old entry. Wait.
            return []
        dtos: list[TradeDTO] = []
        for instrument_id in sorted(self._instruments_in_play(cache)):
            dto = self._project_one(cache, instrument_id, now_ns)
            if dto is not None:
                dtos.append(dto)
        return dtos

    def _project_one(self, cache, instrument_id: str, now_ns: int) -> TradeDTO | None:
        key = self._key(instrument_id)
        pos_id = PositionId(f"{instrument_id}-{self._strategy_str}")
        pos = cache.position(pos_id)  # open, closed, or None
        snapshots = list(cache.position_snapshots(pos_id))
        working = self._working_orders(cache, instrument_id)
        # Only ENTRY/re-entry orders bridge a flat; protective/reduce-only exits do NOT keep a flat cycle alive.
        entry_orders = [o for o in working if not o.is_reduce_only]

        pos_open = pos is not None and pos.is_open

        # --- state ---
        if pos_open:
            state = "HELD"
        elif entry_orders:
            state = "ARMED"
        else:
            state = "CLOSED"

        prior = self._active.get(key)
        if prior is None:
            if state == "CLOSED":
                # Nothing live and we weren't tracking it → already history, emit nothing.
                return None
            # New cycle: anchor opened_ts on the first open leg if there is one, else the earliest live entry
            # order (ARMED before any fill). Stored once → stable across later legs.
            opened_ts = self._mint_opened_ts(pos_open, pos, entry_orders)
            cycle_id = (
                f"{self._account_id}:{self._client_id}:{instrument_id}:{self._strategy_str}:{opened_ts}"
            )
            prior = _ActiveCycle(cycle_id=cycle_id, opened_ts=opened_ts)
            self._active[key] = prior

        # Protective orders are resolved HERE, where `cache` is in scope, and passed down — `_build_dto`
        # never receives the cache (#289: reaching for it there was a NameError that killed the whole
        # projection in production while the unit tests passed, because they exercised the helper and not
        # this seam).
        protective = self._protective_orders_on_instrument(cache, instrument_id)
        dto = self._build_dto(instrument_id, prior, state, pos, snapshots, working, protective, now_ns)

        if state == "CLOSED":
            self._active.pop(key, None)  # terminal — next open is a new cycle
        return dto

    @staticmethod
    def _mint_opened_ts(pos_open: bool, pos: Position | None, entry_orders: list) -> int:
        if pos_open and pos is not None:
            return pos.ts_opened
        if entry_orders:
            return min(o.ts_init for o in entry_orders)
        # Flat with only a just-closed position (rare mint path) → its open ts.
        return pos.ts_opened if pos is not None else 0

    def _cycle_legs(self, instrument_id: str, pos, snapshots: list, opened_ts: int) -> list[CycleLeg]:
        """Every leg of THIS cycle, deduped by ts_opened (a leg is unique within a position_id by its open time)
        with precedence restored < live snapshot < current position — the current native `pos` is the freshest
        state and MUST win, so a restored/snapshot leg that is ALSO the current closed position isn't counted
        twice (codex-flagged double-count). Native snapshots accumulate across cycle boundaries on the shared
        position_id, so filter to legs opened at/after this cycle opened."""
        pos_id = f"{instrument_id}-{self._strategy_str}"
        by_ts: dict[int, CycleLeg] = {leg.ts_opened: leg for leg in self._restored.get(pos_id, [])}
        for s in snapshots:  # live in-memory snapshot beats a restored leg on the same ts_opened
            by_ts[s.ts_opened] = CycleLeg.from_position(s)
        if pos is not None:  # the current position is the freshest native state → wins over snapshot/restored
            by_ts[pos.ts_opened] = CycleLeg.from_position(pos)
        return [leg for ts, leg in by_ts.items() if ts >= opened_ts]

    def _build_dto(
        self, instrument_id: str, cycle: _ActiveCycle, state: str, pos, snapshots: list, working: list,
        protective: list, now_ns: int,
    ) -> TradeDTO:
        legs = self._cycle_legs(instrument_id, pos, snapshots, cycle.opened_ts)
        realized = self._sum_realized(legs)
        # Monotonic fold clock: strictly advance per emit (max(engine now, prior+1)) so every transition —
        # including a no-fill ARMED→CLOSED that carries no native timestamp — out-orders the prior durable
        # write. Falling back to a native max would leave a cancel-close at 0 and let the envelope guard reject
        # the terminal state (codex-flagged). Also gives a total order when two events share a ns.
        native_ts = self._last_event_ts(pos, legs, working)
        cycle.last_event_ts = max(now_ns, native_ts, cycle.last_event_ts + 1)
        last_event_ts = cycle.last_event_ts

        pos_open = pos is not None and pos.is_open
        if pos_open:
            side = pos.side.name  # LONG | SHORT
            quantity = float(pos.quantity)
            avg_px_open = float(pos.avg_px_open)
        else:
            side = "FLAT"
            quantity = 0.0
            avg_px_open = None

        closed_ts = None
        if state == "CLOSED":
            # A CLOSED cycle ALWAYS gets a closed_ts (so load_active can't reload it as live) — native close ts
            # when a position closed it, else the fold clock (a no-fill ARMED cancel has no native close).
            closed_ts = pos.ts_closed if (pos is not None and pos.ts_closed is not None) else last_event_ts

        return TradeDTO(
            account_id=self._account_id,
            client_id=self._client_id,
            instrument_id=instrument_id,
            strategy_id=self._strategy_str,
            cycle_id=cycle.cycle_id,
            manager_id=None,  # no managers in v0
            state=state,
            side=side,
            quantity=quantity,
            # Dual-lens (#77): deployed = HELD with a live position (counts toward %-deployed); engaged = any
            # non-terminal cycle (appears in the managed-names list). A flat ARMED name is engaged, not deployed.
            is_capital_deployed=(state == "HELD" and quantity != 0.0),
            is_engaged=(state != "CLOSED"),
            avg_px_open=avg_px_open,
            realized_pnl=str(realized),  # Money string, e.g. "1000.00 USD"
            leg_count=len(legs),
            opened_ts=cycle.opened_ts,
            closed_ts=closed_ts,
            last_event_ts=last_event_ts,
            # Own orders PLUS any strategy's protective orders on this instrument (#289). The cycle logic
            # above still uses `working` alone; only what the UI reads for coverage is widened.
            working_orders=[self._order_dto(o) for o in _dedupe_orders(working + protective)],
        )

    @staticmethod
    def _sum_realized(legs: list[CycleLeg]) -> Money:
        """Cycle realized = Σ each leg's native realized_pnl (verified by the #76 replay harness: closed legs
        keep their realized in the snapshot, the reopened leg resets to 0). Rebuilt as a Money so the DTO string
        carries the currency's precision ("1000.00 USD", not "1000"). Currency from the legs; USD when flat/empty."""
        total = Decimal(0)
        currency = USD
        for leg in legs:
            money = leg.realized_pnl
            if money is not None:
                total += money.as_decimal()
                currency = money.currency
        return Money(total, currency)

    @staticmethod
    def _last_event_ts(pos, legs: list[CycleLeg], working: list) -> int:
        # Most recent event folded into this cycle: the current position's last event, each closed leg's close
        # time, and any working order's last event.
        candidates: list[int] = []
        if pos is not None and pos.ts_last is not None:
            candidates.append(pos.ts_last)
        candidates += [leg.ts_closed for leg in legs if leg.ts_closed is not None]
        candidates += [o.ts_last for o in working if o.ts_last is not None]
        return max(candidates) if candidates else 0

    @staticmethod
    def _order_dto(order) -> WorkingOrderDTO:
        return WorkingOrderDTO(
            client_order_id=order.client_order_id.value,
            side=order.side.name,
            order_type=order.order_type.name,
            quantity=float(order.quantity),
            leaves_qty=float(order.leaves_qty),
            price=float(order.price) if order.has_price else None,
            trigger_price=float(order.trigger_price) if order.has_trigger_price else None,
            time_in_force=order.time_in_force.name,
            status=order.status.name,
            ts_last=order.ts_last,
            # THE DTO MUST CARRY WHAT THE ENGINE PUBLISHES (#233/#322/#336, three times). A field added
            # to the model and not filled here is a field the UI reads as absent forever.
            tags=list(order.tags or []),
        )
