"""Typed API contract — Pydantic DTOs that mirror the Nautilus domain objects.

These are the ONLY shapes that cross the REST/WS boundary. The generated TS client derives
its types from these via OpenAPI, so any rename here flows to the UI on the next `gen:api`.
Keep them flat and JSON-native (no Nautilus value objects leak past this module).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PositionDTO(BaseModel):
    """A single open/closed position read from the Nautilus cache."""

    instrument_id: str
    side: str  # FLAT | LONG | SHORT
    quantity: float
    avg_px_open: float
    realized_pnl: str  # Money string (currency-aware), e.g. "0.00 USD"
    strategy_id: str
    #: ns of the most recent event on this position — the staleness lock a transfer is validated
    #: against (#785). Defaulted so an older engine frame still parses; the engine always sends it.
    ts_last: int = 0


class BarDTO(BaseModel):
    """One OHLCV bar as emitted by a Nautilus `on_bar` event."""

    instrument_id: str
    ts_event: int  # epoch nanoseconds
    open: float
    high: float
    low: float
    close: float
    volume: float


class FillDTO(BaseModel):
    """An order fill from a Nautilus `OrderFilled` event."""

    instrument_id: str
    side: str  # BUY | SELL
    quantity: float
    price: float
    ts_event: int  # epoch nanoseconds
    order_type: str
    strategy_id: str


class OrderDTO(BaseModel):
    """A working/terminal order snapshot for the Orders blotter (#33). Mirrors engine `_order_frame`.
    `trigger_price` is set only on stop-type orders; `avg_px` is null until a fill."""

    client_order_id: str
    venue_order_id: str | None = None
    instrument_id: str
    side: str  # BUY | SELL
    order_type: str  # MARKET | LIMIT | STOP_MARKET | STOP_LIMIT
    quantity: float
    filled_qty: float
    leaves_qty: float
    price: float | None = None  # limit price
    trigger_price: float | None = None  # stop trigger
    time_in_force: str  # DAY | GTC | AT_THE_OPEN | AT_THE_CLOSE | IOC | FOK
    status: str  # ACCEPTED | SUBMITTED | FILLED | CANCELED | REJECTED | …
    avg_px: float | None = None  # average fill price
    ts_last: int  # epoch ns of the last event on this order
    strategy_id: str
    tags: list[str] = []
    reason: str | None = None  # deny/reject reason (OrderDenied/OrderRejected) — surfaced to the order detail


class WorkingOrderDTO(BaseModel):
    """A live working order attached to a trade cycle (an ARMED entry/re-entry, or a protective leg). A small
    summary — the Orders blotter (#33) carries the full OrderDTO; this is what the cycle surface needs."""

    client_order_id: str
    side: str  # BUY | SELL
    order_type: str  # MARKET | LIMIT | STOP_MARKET | STOP_LIMIT
    quantity: float
    leaves_qty: float
    price: float | None = None
    trigger_price: float | None = None
    time_in_force: str
    status: str
    ts_last: int  # epoch ns of the last event on this order
    #: The ORDER'S OWN TAGS, as the engine set them. Carried because the UI labels an entry-floor stop
    #: (#872) from `mode:entry_floor` rather than re-deriving the kind from `order_type` — a STOP_MARKET
    #: is also a bracket leg, a PEAK stop and an operator's own sell, and a second derivation of "is
    #: this a floor" would drift from the one that placed it. Empty, never null: "no tags" is a fact.
    tags: list[str] = []


class TradeDTO(BaseModel):
    """A trade CYCLE (#73) — the engine projection over native Nautilus positions + snapshots + working orders,
    NOT a parallel ledger. Lifetime = open→closed spanning intra-cycle flats. Carries the ADR canonical identity
    `(account_id, client_id, instrument_id, strategy_id, cycle_id)` from round 1 so the UI never keys off the
    native position_id (a strategy key, reused across cycles). `cycle_id` is OPAQUE — do not parse it.

    P&L is native: `realized_pnl` = Σ(closed snapshot legs' realized) + current leg's realized. `avg_px_open`
    is the CURRENT leg only (null while flat). `manager_id` is nullable (no managers in v0)."""

    account_id: str
    client_id: str  # the EXECUTION client (ALPACA/IB), not the data client
    instrument_id: str
    strategy_id: str
    cycle_id: str  # opaque; {account}:{client}:{instrument}:{strategy}:{open_anchor_ts} — never parse it
    manager_id: str | None = None
    state: Literal["HELD", "ARMED", "WATCH", "CLOSED"]
    side: str  # LONG | SHORT | FLAT
    quantity: float
    # Dual-accounting lens (#77), FIRST-CLASS so the UI never re-derives it: `is_capital_deployed` (HELD with a
    # live position) is what counts toward net-liq / %-deployed; `is_engaged` (any non-terminal cycle, incl a
    # flat ARMED name) is what appears in the managed-names list. An ARMED qty-0 row is engaged but NOT deployed.
    is_capital_deployed: bool
    is_engaged: bool
    avg_px_open: float | None = None  # current leg only; null while flat
    realized_pnl: str  # cycle total, Money string e.g. "1000.00 USD"
    # Mark-to-market for a HELD cycle (#26) — so the managed book shows UNREALIZED P&L / value, not just
    # realized (a held name with realized 0 is NOT "flat P&L"). Marked with the last bar close; None while flat
    # or when no price is available.
    last_px: float | None = None
    market_value: float | None = None
    unrealized_pl: float | None = None
    unrealized_plpc: float | None = None
    leg_count: int  # closed snapshot legs of THIS cycle + (1 if a leg is currently open)
    # The cycle-open anchor (ns): the first leg's ts_opened, OR — if the cycle armed before its first fill —
    # the arming entry order's ts_init. Stable across the ARMED→HELD→re-entry transitions of one cycle.
    opened_ts: int
    closed_ts: int | None = None  # cycle end (not just last position close), ns
    last_event_ts: int  # ns of the most recent event folded into this cycle
    working_orders: list[WorkingOrderDTO] = []
    #: Does the BROKER have a protective sell resting on this instrument (#285)? Three-state: None means
    #: the broker has not been asked. The cache cannot answer it — the engine holds orders as REJECTED
    #: that Alpaca reports OPEN, and Nautilus refuses REJECTED -> ACCEPTED, so they never recover.
    broker_protected: bool | None = None
    #: The BROKER's cost basis, and whether it CONTRADICTS the engine's (#370). Three-state like
    #: `broker_protected`: None means the broker has not been asked, which must not read as agreement.
    #: WHD reported +$263.84 unrealized against the broker's +$9.52 — $254.32 of gain that did not exist —
    #: because reconciliation detected the divergence, blamed the venue, and left the wrong value in place.
    #: Published so the UI can say the number is contested rather than render it as settled.
    venue_avg_px: float | None = None
    basis_contested: bool | None = None


class PositionsResponse(BaseModel):
    """REST `GET /positions` payload."""

    positions: list[PositionDTO]


class TradesResponse(BaseModel):
    """REST `GET /trades` payload — the trade-cycle plane (#73)."""

    trades: list[TradeDTO]
    #: What the engine reported about this projection (#298): "ok" | "seeding" | "failed", or None from a
    #: node with no engine. The UI needs it to tell a genuinely flat book from a broken one — rendering
    #: those identically is how an empty tile stood in for eight held positions on 2026-08-14.
    status: str | None = None
    error: str | None = None
    #: Realized P&L for the session, from NATIVE closed positions (#233). A Pydantic response model
    #: DROPS keys it does not declare, so the engine publishing this was not enough — the field reached
    #: Redis and was filtered out at the REST boundary, and the tile went on reading $0.00. Declared
    #: here so it survives the hop.
    #:
    #: `{by_strategy, closed_count, total, partial_open, is_partial}`. Untyped beyond `dict` because the
    #: engine owns the shape and a second declaration of it here would be a second thing to keep in step.
    realized_session: dict | None = None
    #: Realized P&L per period, from the broker's fills (#322): `{"1W": {total, closed_count, unmatched,
    #: is_partial}, ...}`. Declared for the same reason as the line above — an undeclared key does not
    #: fail, it silently disappears here while the engine goes on publishing it.
    #:
    #: `None` means NOT YET SWEPT, and is not the same as every period being zero.
    #:
    #: Since #846 this is the LEGS derivation on every venue (`source: "legs"`); the broker sweep moved to
    #: `realized_periods_swept`. Both declared — an undeclared key vanishes here (#233/#322/#336).
    realized_periods: dict | None = None
    realized_periods_swept: dict | None = None
    realized_legs: dict | None = None
    #: Per-lane net invested per ET session from the engine's cache orders (#699 a): the flows half
    #: of `net(W) = ΔMV − invested(W)`. None on a frame from an engine that predates the field.
    lane_flows: dict | None = None


class ExternalActivityDTO(BaseModel):
    """Broker activity NOT originated by the cockpit (#79) — a native-EXTERNAL order/position (manual at the
    broker, or reconciliation-generated) or a foreign strategy's. QUARANTINED: visible so it can't silently
    join a strategy's P&L, but never attached to a cycle (that's the coordinator's job, #80). Strategy P&L stays
    Σ(TradeDTO) for OWNED strategies only; this contributes only to the broker-vs-cycle net discrepancy."""

    account_id: str
    client_id: str
    instrument_id: str
    source: Literal["ORDER", "POSITION"]
    strategy_id: str  # the non-owned id (EXTERNAL, or a foreign strategy)
    origin: Literal["VENUE", "RECONCILIATION", "FOREIGN", "UNKNOWN"]
    status: str = "QUARANTINED"
    side: str  # LONG | SHORT | FLAT (position) or BUY | SELL (order)
    quantity: float
    realized_pnl: str | None = None  # positions only, Money string
    client_order_id: str | None = None  # orders only
    order_status: str | None = None  # orders only
    # Broker financials for POSITION rows (#26) — the cockpit's own cache can't compute unrealized P&L without
    # quotes, so these come straight from the broker so the portfolio shows cost basis, value, and P&L, not
    # just a bare quantity. None for ORDER rows or when the broker snapshot hasn't arrived.
    avg_px: float | None = None
    last_px: float | None = None
    market_value: float | None = None
    unrealized_pl: float | None = None
    unrealized_plpc: float | None = None
    ts_last: int  # ns of the most recent event
    #: THE BROKER'S quantity for this symbol (#807 item 3). THREE STATES: a number (the broker answered),
    #: 0.0 (the broker answered and holds none — this row is a phantom, not a holding), None (the
    #: broker has not answered yet). On 2026-09-09 four "reconciled — tap to move to a strategy" rows
    #: were stock the account did not own; moving one would have claimed shares that do not exist.
    venue_qty: float | None = None


class LiquidateLaneRequest(BaseModel):
    """`POST /strategies/{strategy_id}/liquidate` body (#922) — flatten a LANE's whole book, now.

    `expected_positions` / `expected_total_qty` are the leash: what the caller SAW. The engine takes ONE
    snapshot of the lane's open positions, refuses if it disagrees, and sweeps exactly that snapshot.
    `invoked_by` and `reason` are provenance and are required — this is the one command that can flatten a
    lane deliberately, and "why is MOMENTUM flat" must not be archaeology. Outside regular hours the engine
    writes LIQUIDATING and submits nothing (`deferred_to_next_open`); invoke again in hours.
    """

    expected_positions: int = Field(ge=0)
    expected_total_qty: float = Field(ge=0)
    invoked_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class FlattenRequest(BaseModel):
    """`POST /positions/flatten` body (#170 first slice).

    `expected_side`/`expected_qty` are what the human SAW when they confirmed. The engine sizes the close
    from its own live cache and rejects if reality has moved — an exit sized at render time can reverse the
    position instead of closing it. No price or order type: in regular hours this is a market order; outside
    them there usually isn't a reliable two-sided quote to price into, so the engine ATTACHES a
    `deferred_flatten` manager (#55, `GET /managers`) instead and submits at the next open — see `api.flatten`
    for why the engine queues rather than prices into a bad spread.

    `cycle_id` (optional) is the cycle the operator was looking at when they confirmed — if the position's
    cycle changes while a deferred flatten sits queued (closed and reopened), the manager refuses to apply
    against the new one rather than treating a coincidental match as the original intent."""

    instrument_id: str
    strategy_id: str
    expected_side: str  # LONG | SHORT — the side the operator believed they held
    expected_qty: float | None = None
    cycle_id: str | None = None


class AttachManagerRequest(BaseModel):
    """`POST /managers/attach` body (#47's `stop_reenter_watch` is the first UI-reachable caller — the
    primitive itself, `_handle_attach_manager`, is generic across every manager kind and reusable by #46/#38
    later without a new endpoint).

    `params` is kind-specific and validated server-side by that kind's `validate_params` (`api.managers`) —
    this DTO stays a thin envelope, not a per-kind schema, so a new kind never needs a new request model.
    `leash` defaults to AUTO — the only leash this session's semi-auto UX actually exercises (arming IS the
    human gate, no per-action confirm step; see `docs/design-system/STYLE_GUIDE.md` Layer 5)."""

    kind: str
    instrument_id: str
    strategy_id: str | None = None  # defaults to this engine's own strategy id server-side
    cycle_id: str | None = None
    # AUTO only. The reducer still models CONFIRM/ALERT and is tested on all three, but `mg.claim` has no
    # leash predicate, so a non-AUTO row would be applied exactly like AUTO — and the engine now refuses
    # them outright. Advertising them here would put two safety modes in the OpenAPI schema and in every
    # generated client that this system does not implement. (codex review, #255.)
    leash: Literal["AUTO"] = "AUTO"
    params: dict


class CancelManagerRequest(BaseModel):
    """`POST /managers/{manager_id}/cancel` body — the toggle's OFF path. Empty on purpose: `manager_id`
    comes from the path, and cancellation needs no other input (the engine refuses to touch an
    already-terminal manager, see `_handle_cancel_manager_command`)."""


class ManagerDTO(BaseModel):
    """One manager instance (#55) and how far it's got. Read directly from Postgres — the ack for the
    original attach command expires long before an overnight manager fires, so a screen re-opened hours
    later can't rely on it."""

    manager_id: str
    kind: str
    instrument_id: str
    strategy_id: str
    # The cycle this manager was attached against; None for pre-#68 migrated rows. A screen matching managers
    # to a position by (kind, instrument, strategy) alone can otherwise let an old FAILED manager from a
    # CLOSED cycle mask a newer ARMED one on the position's current cycle (code review) — the UI must prefer
    # an exact cycle_id match and fall back to null only when nothing on the current cycle exists.
    cycle_id: str | None = None
    leash: Literal["AUTO", "CONFIRM", "ALERT"]
    state: Literal["ARMED", "PROPOSED", "APPROVED", "APPLYING", "APPLIED", "FAILED", "CANCELLED"]
    params: dict
    # Populated only for FAILED — the detail from its most recent FAILED manager_event, so a rejected
    # deferred flatten shows WHY, not just that it failed.
    error: str | None = None
    #: WHEN (#400). The columns existed on `Manager` from the start and this response model simply never
    #: declared them, so every row arrived undated — the fourth time a published field has been dropped
    #: at a DTO boundary in this codebase (#233 / #322 / #336 / here).
    #:
    #: The cost was not cosmetic. A `peak_watch` row that FAILED on 2026-08-11 with an error fixed 38
    #: minutes later rendered identically to `A.XNYS`'s `stop_reenter_rearm` that FAILED at 18:20 on
    #: 2026-08-20 — and reading the undated list produced a report that PEAK was currently broken, which
    #: it was not, while the two-hour-old failure that mattered went unnoticed until Postgres was queried
    #: directly.
    #:
    #: `updated_at` is the terminal-state timestamp for a FAILED or CANCELLED row: the model sets
    #: `onupdate=func.now()`, and a terminal row is not touched again.
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ManagersResponse(BaseModel):
    """REST `GET /managers` payload."""

    managers: list[ManagerDTO]


class TransferDTO(BaseModel):
    """One internal position transfer (#80 spin-off) and how far it got.

    A transfer MOVES quantity between strategies with paired internal fills — no order reaches the venue and
    the broker net is unchanged. `state` is the outbox progress; anything other than COMPLETED/FAILED means a
    transfer is still in flight or was interrupted and awaits recovery."""

    transfer_id: str
    instrument_id: str
    source_strategy_id: str
    target_strategy_id: str
    side: str
    quantity: float
    pricing_mode: Literal["MARKET", "CARRY_OVER"]
    transfer_px: float
    source_avg_px: float
    state: Literal["PREPARED", "SOURCE_APPLIED", "DEST_APPLIED", "COMPLETED", "FAILED"]
    reason_code: str
    error: str | None = None


class TransfersResponse(BaseModel):
    """REST `GET /transfers` payload."""

    transfers: list[TransferDTO]


class TransferRequestBody(BaseModel):
    """`POST /transfers` body.

    No price field on purpose: the engine sources it. Whoever picks the price picks which strategy keeps the
    P&L, so an operator-supplied price would make transfers a P&L-shifting tool. `actor` is likewise stamped
    server-side."""

    instrument_id: str
    target_strategy_id: str
    quantity: float
    side: str = "LONG"
    source_strategy_id: str = "EXTERNAL"
    # CARRY_OVER: basis migrates, nothing realized — the target inherits existing unrealized P&L.
    # MARKET: source crystallizes P&L to the handoff, target starts clean.
    pricing_mode: Literal["MARKET", "CARRY_OVER"] = "CARRY_OVER"
    reason_code: str = "unspecified"


class ExternalActivityResponse(BaseModel):
    """REST `GET /external-activity` payload — the quarantine plane (#79)."""

    external: list[ExternalActivityDTO]


class BracketRequest(BaseModel):
    """A native Nautilus bracket (#34): entry + protective STOP + take-profit TARGET (all required — a
    bracket is the full trio; entry+stop-only is a separate feature). The api mints the entry/stop/target
    client_order_ids; Nautilus's OrderEmulator manages the OTO/OCO/OUO contingencies."""

    instrument_id: str
    side: str  # BUY | SELL (entry side; the protective legs exit the opposite way)
    quantity: float = Field(gt=0)
    entry_order_type: str = "market"  # market | limit
    price: float | None = Field(default=None, gt=0)  # entry limit price (required for a limit entry)
    stop_trigger: float = Field(gt=0)  # protective stop trigger — required
    target_price: float = Field(gt=0)  # take-profit limit — required (native bracket needs a TP)
    time_in_force: str = "day"


class OrdersResponse(BaseModel):
    """REST `GET /orders` payload — the Orders blotter (working + recent terminal)."""

    orders: list[OrderDTO]


class CommandResponse(BaseModel):
    """Response to a command POST (submit/bracket/cancel/modify) — #39. `ok` means the command was ENQUEUED
    on the bus (not that the order was placed). Correlate `command_id` via `GET /commands/{id}` for the
    engine's accept/reject; `client_order_id` correlates the broker/order lifecycle (Orders blotter)."""

    ok: bool = True
    command_id: str
    client_order_id: str | None = None


class CommandStatusResponse(BaseModel):
    """Engine ack for a UI command (#39). `status`: `pending` (no ack yet — the caller applies its own
    timeout → `unknown`, never a false reject) · `accepted` (engine took it; the Orders blotter is
    authoritative thereafter) · `rejected` (engine refused — disarmed/malformed; `error` carries why).

    `detail` is an informational message on an ACCEPTED command — e.g. a flatten that was queued rather than
    submitted immediately (#170 spin-off). Kept distinct from `error`: a message on a command that succeeded
    is not an error, and conflating them would make "accepted with an error string" read as a rejection."""

    command_id: str
    status: Literal["pending", "accepted", "rejected"]
    command_type: str | None = None
    error: str | None = None
    detail: str | None = None


class ModifyOrderRequest(BaseModel):
    """PATCH an existing working order → a `modify_order` command. All fields optional; only the provided
    ones change (Nautilus keeps the rest). Fail-closed: provided values must be > 0, and at least one
    field must be set (an empty modify is a no-op that should never reach the engine)."""

    quantity: float | None = Field(default=None, gt=0)
    price: float | None = Field(default=None, gt=0)  # new limit price
    trigger_price: float | None = Field(default=None, gt=0)  # new stop trigger

    @model_validator(mode="after")
    def _at_least_one(self) -> ModifyOrderRequest:
        if self.quantity is None and self.price is None and self.trigger_price is None:
            raise ValueError("modify requires at least one of quantity/price/trigger_price")
        return self


# --- WebSocket envelope -------------------------------------------------------
# Every WS frame is one of these tagged messages. `type` discriminates the union
# so the UI can switch on it without sniffing payload shape.

class BarMessage(BaseModel):
    type: Literal["bar"] = "bar"
    data: BarDTO


class FillMessage(BaseModel):
    type: Literal["fill"] = "fill"
    data: FillDTO


class StatusMessage(BaseModel):
    type: Literal["status"] = "status"
    data: str  # e.g. "replay_start", "replay_end"


class SnapshotData(BaseModel):
    """Full bar/fill history for every instrument — sent once on WS connect."""

    bars: list[BarDTO]
    fills: list[FillDTO]


class SnapshotMessage(BaseModel):
    type: Literal["snapshot"] = "snapshot"
    data: SnapshotData


WSMessage = BarMessage | FillMessage | StatusMessage | SnapshotMessage


# --- Health (#26) -------------------------------------------------------------


class SubsystemHealth(BaseModel):
    """One dependency's live connectivity. `name` = redis | postgres | engine; `detail` carries the error
    when down (empty when ok)."""

    name: str
    #: `None` = UNKNOWN, and it is not a synonym for down (#859). The `lanes` subsystem computed its
    #: verdict as `not lanes_absent`, so an absent engine frame — an empty dict — became `ok: True`:
    #: a positive verdict manufactured out of no information. Measured on an Alpaca paper instance 2026-09-11 00:34, that
    #: happened in all 18 of the samples where the bridge was down. A subsystem nobody could ask must
    #: say so; `status` already treats a falsy `ok` as not-ok, so unknown degrades the headline
    #: without ever claiming health.
    ok: bool | None
    detail: str = ""


class DriftEntry(BaseModel):
    """One broker-vs-cache position mismatch (#26) — built by the exec adapter's reconcile pass
    (`providers/alpaca/exec_client.py`), published on the bus, and folded into `HealthResponse` as-is."""

    symbol: str
    broker_qty: float
    platform_qty: float


class SplitDivergencePair(BaseModel):
    """One (strategy, symbol) where the claims ledger and the engine cache disagree (#817).

    Both numbers are carried, never a delta: "BCTROT-004 claims 46, cache attributes 10" tells an
    operator which side to trust and by how much, and a single signed difference does not.
    """

    symbol: str
    strategy_id: str
    claim: float
    cache: float


class LaneBleedDTO(BaseModel):
    """One lane going to cash under a TRADING label (#1098): NO DECISION for `no_decision_sessions`
    sessions while `venue_exits` protective stop-outs shrank its book and it entered nothing."""

    strategy_id: str
    venue_exits: int
    #: `None` = UNKNOWN: a landed own order in the window carried no side (momentum/qc345 terminal
    #: rows), so entries cannot be counted — rendered `entries unknown`, never 0.
    own_entries: int | None
    no_decision_sessions: int
    since: str
    blocked: str
    line: str


class LanesBleedingDTO(BaseModel):
    """THREE STATES (#1098, lead's scope): `lanes == []` with `status == "ok"` means the journal was
    read and no lane bleeds; `status == "unreadable"` carries `lanes: None` and the error — a failed
    read is never an empty list, and never moves the banner by itself."""

    status: str                       # "ok" | "unreadable"
    lanes: list[LaneBleedDTO] | None = None
    error: str | None = None


class SplitDivergenceDTO(BaseModel):
    """The split-divergence surface, with its own status (#817).

    THREE STATES, and the middle one is why the status exists: `pairs == []` with `status == "ok"`
    means "computed, none found"; a non-ok status means a half could not be read and NOTHING was
    computed. An empty list without a status is a claim of health about a book nobody looked at —
    the shape that let eleven pairs stand on ibkr-paper.
    """

    status: str                       # "ok" | "cache_unreadable" | "claims_unreadable"
    pairs: list[SplitDivergencePair] = []
    error: str | None = None


class OwnershipViolation(BaseModel):
    """One lane holding a quantity it is not permitted to hold (#437).

    DELIBERATELY NOT A `DriftEntry`. Drift compares the broker against the cache and its remedy is
    "trust the broker's number". Here the broker AGREES with the netted cache — on 2026-08-30 eight
    mirrored shorts netted to exactly what the broker held, so `reconcile_drift` was correctly empty
    for nine days — and the remedy is the opposite: trust NEITHER lane figure until ownership is
    reconciled. Same file, same response, different claim; folding it into drift would make the
    banner mean less rather than more. Sits beside `reconcile_drift` for the reason
    `unreconciled_orders` does: a book can be perfectly reconciled and still be mis-attributed.
    """

    strategy_id: str
    instrument_id: str
    signed_qty: float


class NetTerms(BaseModel):
    """The two terms the tile subtracts from a lane's live market value (#699 a), and the NAMED reason
    the window is partial. A None term is UNKNOWN — the tile draws an em dash, never a zero."""

    mv_base: float | None = None
    invested: float | None = None
    partial: str | None = None


class WindowBaseResponse(BaseModel):
    """What each lane held at each window's START, so the tile can render a per-lane DELTA (#699).

    `by_period` maps every period to a `{strategy_id: unrealized}` map, or NULL. Every period is
    present: an absent KEY is indistinguishable from a serialisation fault, while an explicit null is
    a statement the tile can render an em dash for and an operator can trust.

    `unreadable` names base dates whose read FAILED, which is a THIRD state and not a variant of the
    other two. A period with no base (`all`) and a base day nobody captured are both gaps; a database
    that would not answer is an outage. All three render the same em dash, and only this field makes
    the difference recoverable — without it, an outage is reported as "no data for that window" and
    nobody ever acts on it.
    """

    by_period: dict[str, dict[str, float | None] | None]
    #: The lanes' MARKET VALUE at each window's base (#699 a): same rows, same day, same three states.
    market_value: dict[str, dict[str, float | None] | None] = {}
    #: `{period: {lane: {mv_base, invested, partial}} | None}` — the two terms the tile subtracts from
    #: the lane's live market value (`net = mv_now − mv_base − invested`), and the NAMED reason a
    #: window is partial. A term is None when unknown; a period is None when it has no base.
    net: dict[str, dict[str, NetTerms] | None] = {}
    #: Which day actually answered each period. A weekend base inherits the preceding Friday's
    #: close, so the requested date and the answering date differ — an operator reading a 1M delta
    #: needs to know whether it was measured from 30 days back or 34.
    base_date: dict[str, str | None] = {}
    #: Base DATES whose read failed — a day to backfill.
    unreadable: list[str] = []
    #: A failure of the assembly ITSELF, which is a different operator action: restart or debug the
    #: api rather than backfill a date. Split from `unreadable` because that field's docstring
    #: promises dates and an "assembly failed: TypeError" string in it is a lie about its own type.
    error: str | None = None


class ProtectionDivergence(BaseModel):
    """One instrument where the broker holds resting protective orders the cache does not know about.

    `cache_holds` carries what the cache believes about those exact ids (e.g. `COID=REJECTED`) rather
    than a count, because the difference between "the cache never saw it" and "the cache marked it
    REJECTED" is the difference between an adoption gap and the 2026-08-22 outage.
    """

    instrument_id: str
    coids: list[str]
    cache_holds: list[str] = []


class FlipPendingRow(BaseModel):
    """One wrong-mode leg that has wanted to flip trail → floor and not, for `passes` consecutive
    protection passes (#907). `last_reason` is the named refusal that stopped it on the latest pass."""

    instrument_id: str
    strategy_id: str
    passes: int
    #: When the CURRENT streak of consecutive deferrals began. RESETS with `passes` — it is not "first
    #: seen ever"; #908's surviving `first_seen` is the one that does not reset. Named so the two
    #: cannot be read as the same fact (coordinator, 2026-09-11).
    streak_started_ns: int
    last_reason: str


class HealthResponse(BaseModel):
    """Connection/health across every system the cockpit depends on (#26).

    `status` IS NOT ONLY CONNECTIVITY, and the docstring said it was until #817. It is "ok" when all
    subsystems are up AND the book can be SIZED from — so an unpriced book (#757), a stale feed, and
    a claims/cache split divergence (#817) each degrade it on their own. The api process answers
    either way. `subsystems` are probed live each request: redis + postgres directly, engine via its
    health frames (the api can't reach the engine across the #20 split). `feed_last_tick_ts` = the
    newest `ts_init` a live data handler saw, monotonic (#917) — the UI ages it (market-hours aware) to tell a live
    feed from a dead one."""

    status: str  # "ok" | "degraded"
    subsystems: list[SubsystemHealth]
    #: HAS THE ENGINE SPOKEN RECENTLY (#859). `consumer.health()`'s own comment tells readers to
    #: distinguish "no divergence" from "no engine" via this field — and it never reached the
    #: response, so that instruction was unfollowable and every list below read as a measurement.
    #: The eighth field to die in that hop after last_equity, realized_session, realized_periods,
    #: next_fire_ns, unpriced_positions, failed_requests and feed_stale.
    #:
    #: A REAL BOOLEAN, never None: this is the field that answers "do we know anything at all", and a
    #: reader cannot interrogate an unknown.
    bridge_ok: bool = False
    #: How many times the bridge went quiet for longer than the staleness threshold, and the worst
    #: such gap, over this api process's whole life. A COUNT AND A MAX because the bridge FLAPS: 9 s,
    #: 3 s and 7 s inside one 30 s span on 2026-09-11 00:34. Not gated on `bridge_ok` — a fact about
    #: the process, not about the current frame.
    bridge_gaps: int = 0
    bridge_gap_max_s: float = 0.0
    feed_last_tick_ts: int  # newest `ts_init` seen by a LIVE data handler, MONOTONIC (#917); arrival where the
    # adapter stamps arrival (IBKR), the datum's own time where it does not (Alpaca). 0 = none yet.
    #: The kind of print `feed_last_tick_ts` is stamped from (#834): "REALTIME" / "DELAYED" for IB,
    #: None where the provider declares none or the bridge is stale. The UI's freshness derivation
    #: needs it — a DELAYED feed is ~15 minutes behind by design, so an age that means "dead" under
    #: REALTIME means "healthy" under DELAYED, and an entry price must be labelled as delayed.
    market_data_type: str | None = None
    # Broker-vs-cache position drift (#26). Non-empty means the broker holds positions the cockpit can't
    # reconcile/display — the UI raises a loud banner so an empty book is never mistaken for a flat account.
    # Empty = in sync.
    reconcile_drift: list[DriftEntry] | None = None
    #: Lanes holding a short in a long-only book (#437). Non-empty means the per-strategy split is
    #: wrong even when `reconcile_drift` is empty and the totals match the broker — the held size
    #: every lane row displays is mis-stated, so the UI must say so rather than draw a clean book.
    #: None means the check could not READ the book — distinct from `[]`, which claims there are
    #: no violations. `signed_qty_of` raises on a shape it cannot understand rather than reporting a
    #: silent all-clear, and that refusal must reach the caller as UNKNOWN instead of as clean.
    ownership_violations: list[OwnershipViolation] | None = []
    #: Legs deferred from flipping, with their consecutive-pass counts (#907). `None` = the engine
    #: frame could not be read; `[]` = nothing pending. A flip deferred forever is a NUMBER here.
    flip_pending: list[FlipPendingRow] | None = None
    #: #873 phase 1 — {contract: {state, module}, dwell, poll_secs, polled_at_ns, lanes: {sid: payload|null},
    #: emit_failures, journal_absent: [sid], journal_broken: {sid: exc}, journal_unchecked: [sid]}.
    #: None = the engine frame could not be read or the build lacks the plane.
    market_aware: dict | None = None
    #: A claim the engine cache CONTRADICTS, per (strategy, symbol) — the BDX shape (#692/#817).
    #:
    #: ON /health BECAUSE DETECTION MUST NOT RIDE ON DELIVERY. Until #817 the only caller of
    #: `split_divergence` was the Telegram alert, inside `if self._enabled():` — so an instance with
    #: notifications off could not look. ibkr-paper stood at eleven divergent pairs and 1,504 shares,
    #: BCTROT-004 claiming positions the engine attributes to EXTERNAL, with every surface green.
    #:
    #: NOT the same question as `book_truth.split_disagrees`, which compares the engine to the VENUE.
    #: This compares the engine to the CLAIMS LEDGER — the thing an exit actually sizes against — and
    #: the two read differently for the same book.
    #:
    #: `status` carries the third state: `pairs == []` means "computed, none found" only when
    #: `status == "ok"`. Optional with a default because existing callers construct HealthResponse
    #: without it.
    split_divergence: SplitDivergenceDTO | None = None
    #: Lanes going to cash under a TRADING label (#1098): NO DECISION for N sessions while venue-side
    #: protective stop-outs shrink the book and nothing enters. Computed by the api from the journal
    #: (`api.lanes_bleeding.scan_bleeding`) — both processes share the database, so no engine frame
    #: field. `status` carries the third state; a non-empty `lanes` degrades the banner, `unreadable`
    #: does not (it is carried here and nowhere else).
    lanes_bleeding: LanesBleedingDTO | None = None
    #: What the realized windows were computed over (#846): `{state, restored, held, live, seed_stopped_at,
    #: complete, error}` — `state` in {never_ran, stopped_early, complete}; `error` when no window has a number.
    #: `complete: false` = the seed's snapshot scan stopped early; the figures understate and say so.
    realized_legs: dict | None = None
    #: Protective orders RESTING AT THE BROKER that this engine cannot see (#454).
    #:
    #: ANSWERS "IS PROTECTION MIS-TRACKED", NEVER "IS PROTECTION PRESENT". The detector iterates BROKER
    #: ORDERS and reports the ones the cache cannot see, so a position with NO resting order produces no
    #: iteration at all — an empty list is what a fully NAKED book returns, and what a fully protected
    #: one returns. A reader treating 0 as "protected" is reading the wrong question, which is exactly
    #: the mistake made on 2026-08-23 while five positions were uncovered.
    #:
    #: The question "is anything protecting this position" is answered by `broker_protected` on the
    #: TRADE plane, which `session_watch.protection_from_trades` reads and the UI's `securedValue`
    #: computes from. Distinct from
    #: `reconcile_drift`, which compares POSITIONS — a book can be perfectly reconciled while every
    #: stop protecting it is invisible to the cache, which is exactly what an Alpaca 503 produced on
    #: 2026-08-22: eleven stops resting, one known.
    #:
    #: Non-empty means EXITS WILL BE REJECTED on `available: 0` — the venue has reserved shares against
    #: orders the engine has written off. That is the operator-facing consequence and it is why the
    #: `coids` travel with it: the symbol alone does not say whether to cancel at the venue or restart.
    #:
    #: Defaulted, because every existing caller constructs this response without it and a required
    #: field would turn a monitoring improvement into an outage.
    protection_divergence: list[ProtectionDivergence] | None = None
    #: Positions whose protection was released for an exit that then never landed (#546): the venue
    #: rejected the sell, or no sell ever reached it (a local refusal emits no order event at all).
    #: FOURTH INSTANCE of the DTO-drops-published-fields class (#233/#322/#336) — the engine
    #: published this and `/health` ate it, so both #546 halves reported into a void. Typed loosely
    #: because the engine owns the shape and a stricter model here is how the field gets dropped
    #: again the next time the engine adds a key.
    naked_after_reject: list[dict] | None = None
    #: Held instruments the engine cannot price (#757). An input to protection sizing, exit sizing
    #: and every displayed P&L — its absence was a log line while half a book was unpriceable and
    #: `status` read ok.
    unpriced_positions: list[str] | None = None
    #: What the engine ASKED the outside world for and did not get, coalesced with a count and the
    #: venue's own words. A request that failed is not one never made.
    failed_requests: list[dict] | None = None
    #: Requested-versus-bound subscriptions, with the NAMES of the dark ones (#618). A pair of counts
    #: alone could not say which 117 of 392 were silent, and that was the whole cost of the ticket.
    #: Typed loosely for the same reason as `naked_after_reject`: the engine owns the shape, and a
    #: stricter model here is how the field gets dropped the next time a key is added.
    subscriptions: dict | None = None
    #: Cache-versus-venue per instrument, with the names of what disagrees (#758). Typed loosely for
    #: the same reason as its siblings: the engine owns the shape.
    book_truth: dict | None = None
    #: Fills the engine fabricated during reconciliation, counted per instrument, with the lane where
    #: one could not land — the phantom's mint, which logged at INFO among thousands of lines.
    inferred_fills: list[dict] | None = None
    #: Fills that arrived for an order the cache holds TERMINAL (#807 item 4) — the second the
    #: position plane starts diverging from the order plane. `book_truth.TerminalFills.as_rows()`.
    fills_on_terminal_orders: list[dict] | None = None
    #: Findings the registry above could not keep past its cap (counted, never hidden).
    fills_on_terminal_orders_dropped: int | None = None
    #: IB shortability plane (#857): {subscribed, answered, stale, never, state} — three states —
    #: or None on a node that has no such plane (every non-IBKR data provider).
    shortable: dict | None = None
    #: Targeted venue reads and post-submit lookups the venue did not ANSWER, counted per attempt since
    #: the exec adapter started (#354). THREE STATES: None = the adapter has not reported, 0 = reported
    #: and none, n = n unanswered attempts. Process-local; health-only; no alert threshold yet.
    venue_unanswered_lookups: int | None = None
    #: The OBSERVERS themselves (#758): declared / ok / failing / never ran, plus any absorbed
    #: failures with their cause. A check that fails must eventually report itself.
    observations: dict | None = None
    #: What the last log-compaction pass did (#758). None until it has run once — which is not the
    #: same as "nothing to do".
    log_compaction: dict | None = None
    #: Whether the feed is stale ON TRADING TIME, computed by the ENGINE because it owns the venue
    #: calendar. THREE STATES: True, False, and None for "unknowable" — a boot before the open, or a
    #: day the venue never described. `app._feed_stale` computed this and never passed it, so every
    #: reader got None regardless (#758 class, eighth occurrence).
    feed_stale: bool | None = None
    #: Per-lane arming, and when each lane next decides. Published by the engine and forwarded by the
    #: consumer, but absent from this model — so `/health` answered None whatever the engine said,
    #: which is indistinguishable from "no lane is armed" and nearly caused a needless restart of an
    #: engine with armed lanes on 2026-09-01 (#768).
    armed_lanes: dict | None = None
    next_fire_ns: dict | None = None
    #: Lanes that SHOULD exist and do not (#539) — the node is the only thing that knows.
    lanes_absent: dict | None = None
    #: AUTOMATED lanes registered on the node, and how many are actually RUNNING (#454).
    #:
    #: NAMED, not `strategies_*`, because the denominator is not what a reader assumes and the old name
    #: proved it: five strategies log RUNNING (MANUAL, MOMENTUM, BCTROT, QC345, TECHIVOL) while this
    #: reads 4/4. MANUAL-001 is discretionary — always live, never scheduled, and not registered as a
    #: sibling — so counting it would make `4/5` the healthy state and a lane-down alarm unreadable.
    #:
    #: The number was correct and the NAME was not falsifiable by a reader: the author of the field
    #: misread his own 4/4 as "all five running" in a message to the one person able to catch it. `None` means the health
    #: frame is stale — unknown, not zero. A boot where nothing starts already self-reports (no frame
    #: is published at all); this catches the PARTIAL start, where the feed runs and a trading strategy
    #: does not, and every surface stays green while no lane can trade.
    automated_lanes_registered: int | None = None
    automated_lanes_running: int | None = None
    #: Ways the stack contradicts its own instructions — a lane set to TRADING that cannot trade.
    #: Empty means none FOUND, which is not the same as none existing: an unreadable input yields
    #: nothing rather than a guess. See `api/inert.py` for the incident that produced it.
    inert: list[str] | None = None

    #: WHAT CODE IS ACTUALLY RUNNING, as content rather than as a claim (#486).
    #:
    #: `*_sha` is stamped at BUILD time and is a claim: on 2026-08-22 a stale-tree build shipped
    #: `98d264c` stamped as itself while `80f6571` was intended, and the build log was byte-identical
    #: to a correct one. `68201a1-dirty` told us a tree was dirty and never what was in it. A deploy
    #: gate comparing labels passes trivially for a build that lies about itself.
    #:
    #: `*_digest` is hashed from the installed bytes and cannot. Two of these are two DERIVATIONS of
    #: one fact, and their disagreement is the signal.
    #:
    #: SERVED UNCONDITIONALLY, INCLUDING WHEN THEY AGREE. A field that appears only when something is
    #: wrong is a field nobody knows the normal value of — which is how `68201a1-dirty` was read past
    #: twice in one weekend. `None` means the measurement was UNAVAILABLE, which is a third state and
    #: never the same as a mismatch.
    #:
    #: NOT THE WHOLE ENVIRONMENT: a digest covers its own package and not its dependencies, so two
    #: identical digests can still run different `nautilus_trader` builds.
    cockpit_sha: str | None = None
    cockpit_digest: str | None = None
    strategies_sha: str | None = None
    strategies_digest: str | None = None


# --- Live price (#28) ---------------------------------------------------------


class AccountDTO(BaseModel):
    """Account snapshot (#41) — Alpaca's own equity (net-liq / portfolio_value) / free cash / buying_power /
    multiplier, for %-of-equity order sizing + affordability gating. `multiplier` = 1 cash, 2/4 margin/PDT
    (buying_power ≈ multiplier × equity). Falls back to a Nautilus-derived estimate on a synthetic node."""

    equity: float
    cash: float
    buying_power: float
    multiplier: float = 1.0
    #: Market value of long holdings, from the BROKER (#310). Without it the BOOK tile had to derive
    #: DEPLOYED from our own trade projection, so a panel showing DEPLOYED, CASH and LIQUIDATION side by
    #: side mixed two price sources and could not add up: the operator computed 99,978.32 + (-208.42) and got
    #: 99,769.90 against a LIQUIDATION of 99,764.62. Alpaca's own figures satisfy
    #: `cash + long_market_value == equity` exactly, so sourcing all three from the account makes the
    #: panel internally consistent by construction rather than by luck.
    long_market_value: float = 0.0
    #: Previous session's CLOSING equity, so the UI can show NET for 1D (#336). The equity-curve plane
    #: publishes 1W/1M/3M/all and NOTHING for 1D, so without this the hero number on Home's default tab
    #: has no source. `equity - last_equity` is Alpaca's own day P&L, keeping NET(1D) broker-sourced
    #: rather than a figure derived here beside the statement.
    #:
    #: Declared here for the SAME reason as `realized_session` and `realized_periods` on TradesResponse:
    #: a Pydantic model DROPS keys it does not declare. The engine published `last_equity`, it reached
    #: Redis, and the tile still rendered "unknown" — the field was filtered out at this boundary. That
    #: is now the third field lost to this exact seam.
    #:
    #: Optional with a None default, NOT 0.0. Every other numeric field here defaults to zero, and
    #: copying that would be wrong: last_equity=0.0 makes the day P&L read as the ENTIRE account equity
    #: ($100,658.95 instead of $542.67). A plausible wrong number in the hero slot is worse than the
    #: dash it replaces.
    last_equity: float | None = None
    #: THE Δ HALF OF NET (#596). `NET(period) = realized(period) + Δunrealized(period)`, both
    #: measured. Until now Δunrealized was BACK-SOLVED as `net - realized`, which made the panel's
    #: stated identity true by construction: it could not disagree, so it could not detect anything.
    #: On 2026-08-27 every period rendered Δ as exactly minus REALIZED, with NET $0.00.
    #:
    #: `standing` is Σ of the broker's per-position unrealized since entry — the ALL window's delta,
    #: on the assumption that nothing was held before the broker's record begins. `intraday` is Σ of
    #: the broker's own per-position day change; Alpaca reports it, IBKR does not.
    #:
    #: DECLARED HERE BECAUSE A PYDANTIC MODEL DROPS WHAT IT DOES NOT DECLARE, and by this file's own
    #: count that has already cost three fields at this exact boundary. These two would have been the
    #: fourth and fifth: the engine publishes them and the tile would have rendered "unknown".
    #:
    #: None, NOT 0.0, for the same reason as `last_equity` above. Zero asserts "the mark did not
    #: move", which is a much stronger claim than "this venue does not report it" — and on IBKR the
    #: intraday figure is genuinely absent rather than flat.
    unrealized_standing_total: float | None = None
    unrealized_intraday_total: float | None = None
    #: THE UNIT (#518). `AccountDTO` published bare floats, and on ibkr-paper-retired that meant 1,000,015.19
    #: SGD rendered beside USD position values with nothing saying so — Nautilus reports
    #: `base_currency=None` for that account and converts nothing.
    #:
    #: Declared here for the reason the paragraph above already gives about `last_equity`: a Pydantic
    #: model DROPS keys it does not declare. An engine publishing `currency` into a model without
    #: this line would lose it at exactly this boundary, for the FOURTH time.
    #:
    #: Optional with a None default, and deliberately NOT "USD". Unknown is an answer; a wrong unit
    #: stated confidently is worse than an absent one, which is the same reasoning `last_equity` uses
    #: for defaulting to None rather than 0.0.
    currency: str | None = None
    ts: int  # epoch nanoseconds


class QuoteDTO(BaseModel):
    """Latest NBBO bid/ask for an instrument (#40) — the spread/mid plane for order-ticket prefill."""

    instrument_id: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    ts_event: int  # epoch nanoseconds


class PriceDTO(BaseModel):
    """A real-time last trade for one instrument — the live-price plane, decoupled from chart bars."""

    instrument_id: str
    price: float
    size: float
    ts_event: int


class VwapDTO(BaseModel):
    """Session VWAP for one instrument (#182 follow-up, watchlist KPI Phase 2) — RTH-only, reset at the
    ET session boundary. Absent for an instrument until real (non-zero) volume has accumulated this
    session; see `UiFeedStrategy._update_vwap_from_bar`. The engine also emits a `vwap: null` tombstone
    frame at the exact moment a new session starts (same `type: "vwap"` stream kind, not this DTO shape —
    `RedisConsumer` pops the instrument instead of constructing one) so a prior session's value never
    lingers past its own session boundary."""

    instrument_id: str
    vwap: float
    session_date: str  # ET session date (YYYY-MM-DD) this value belongs to
    ts_event: int


class TodayRangeDTO(BaseModel):
    """Today's session high/low + prior session close (#182 follow-up, watchlist KPI Phase 3) — sourced
    from Alpaca's batch snapshot endpoint (`dailyBar`/`prevDailyBar`), refreshed on the watchlist timer's
    cadence (~5s). Each fetch validates `dailyBar`'s own ET date against today's before treating it as
    fresh (codex review: Alpaca's dailyBar can lag "today" — a thin symbol, a holiday, an outage — and
    without that check a prior day's value would keep rendering as current). The engine emits a
    `high: null` tombstone frame (same `type: "today_range"` stream kind, not this DTO shape —
    `RedisConsumer` pops the instrument instead of constructing one) the moment a previously-published
    value ages out without a fresh replacement, so it never lingers past its own day."""

    instrument_id: str
    high: float
    low: float
    prev_close: float
    ts_event: int


class FundamentalsDTO(BaseModel):
    """Company fundamentals (#182 follow-up, watchlist KPI Phase 2) — Market Cap, Beta, EPS (trailing
    annual), trailing P/E (computed client-side of the vendor call: price ÷ EPS), Dividend Amount (last
    paid). Sourced from Financial Modeling Prep, refreshed on its own low-frequency timer (fundamentals
    don't move intraday — no reason to poll on the 5s watchlist cadence). Unlike VWAP/Today's Range this
    has NO tombstone: a fetch failure or a temporarily-missing field just keeps the prior value, which is
    the CORRECT behavior here — yesterday's EPS is still today's EPS (no daily-boundary reset the way a
    session-scoped stat has). `as_of` is FMP's own date for the underlying data (quote timestamp / income
    statement fiscal year end), not the fetch time — lets the UI show how old the underlying number
    actually is, independent of poll cadence.

    Forward P/E and next Dividend Date are deliberately NOT here — deferred pending confirmation of a
    clean FMP field for each (see the metric contract); do not add ad hoc without re-confirming."""

    instrument_id: str
    market_cap: float | None
    beta: float | None
    eps: float | None
    pe: float | None
    dividend_amount: float | None
    as_of: str  # ISO date, FMP's own — e.g. quote's date or the income statement's fiscal year end
    ts_event: int


# --- Orders (#32 / #5) — MANUAL-lane discretionary order ----------------------


class OrderRequest(BaseModel):
    """A discretionary order from the cockpit → a `submit_order` command. Placed only when the engine is
    armed (KUMO_ORDERS_ARMED); the api generates the client_order_id (idempotency key)."""

    instrument_id: str
    side: str  # BUY | SELL
    quantity: float
    order_type: str  # market | limit | stop
    price: float | None = None  # limit price (limit orders)
    trigger_price: float | None = None  # stop trigger (stop orders)
    time_in_force: str = "day"  # day | gtc | opg (on-open) | cls (on-close) | ioc | fok
    extended_hours: bool = False  # pre/post-market — engine enforces limit-only


# --- Watchlist (runtime, Postgres-backed) -------------------------------------


class WatchlistAdd(BaseModel):
    """POST /watchlist body — the instrument to add (canonical TICKER.MIC)."""

    instrument_id: str


# --- Symbol pool (#79 follow-on) ----------------------------------------------
class PoolEntryDTO(BaseModel):
    """One symbol the strategy can rank — or one it is deliberately blocked from ranking."""

    symbol: str
    sources: list[str]
    provenance: str
    meta: dict[str, Any] = Field(default_factory=dict)
    held: bool
    override: Literal["pin", "exclude"] | None = None
    reason: str | None = None


class PoolSourceDTO(BaseModel):
    """Per-source freshness. A failed source is a HARD BLOCK on deciding, not a cosmetic warning."""

    name: str
    status: str
    symbol_count: int
    refreshed_at: str | None = None
    stale: bool
    detail: str | None = None


class PoolResponse(BaseModel):
    count: int
    symbols: list[PoolEntryDTO]
    sources: list[PoolSourceDTO]


class PoolSourceRefreshRequest(BaseModel):
    """A source's WHOLE symbol set, replacing whatever it held.

    `symbols` may be a bare list (`meta` defaults to `{}` per symbol — the `my_watchlist` shape) or a
    mapping of symbol to meta (`ledger_book` carries `opened`/`days_held`).

    `allow_shrink` is a REASON and not a flag. A refresh losing more than half the previous set is
    refused, because a truncated feed must not liquidate a book — but a followed trader genuinely
    going flat is the most significant thing this source can say, and a rule with no escape gets
    bypassed rather than obeyed. The reason is recorded, so an unexplained collapse and a justified
    one do not read the same afterwards.

    `create` must be ASKED FOR. A refresh naming a source that does not exist is refused by default,
    because a typo would otherwise produce a source that is fresh, real and feeds nothing while the one
    it was meant to refresh ages into a hard block on deciding. Seeding a NEW instance is the case that
    needs it — ibkr-paper-retired could not seed its empty pool at all — and saying so is one field rather
    than a second endpoint.
    """

    symbols: list[str] | dict[str, dict] = []
    detail: str | None = None
    create: bool = False
    allow_shrink: str | None = None


class PoolOverrideRequest(BaseModel):
    """POST /pool/override — pin keeps a symbol rankable, exclude blocks and liquidates it."""

    symbol: str
    kind: Literal["pin", "exclude"]
    reason: str = ""


class WatchlistResponse(BaseModel):
    """The current watchlist — instrument ids, oldest-added first."""

    symbols: list[str]


# --- Instrument search (#25) --------------------------------------------------


class InstrumentMatch(BaseModel):
    """One ranked hit from the instrument-search catalog. Flat, JSON-native (crosses REST → TS client)."""

    instrument_id: str  # canonical TICKER.MIC (e.g. "AAPL.XNAS") — same id as positions/bars/watchlist
    symbol: str  # ticker (e.g. "AAPL")
    name: str  # company name (from the venue asset; falls back to symbol if the venue omits it)
    venue: str  # MIC (e.g. "XNAS")


class InstrumentSearchResponse(BaseModel):
    """Ranked instrument-search results, best first, capped at the request's `limit`."""

    results: list[InstrumentMatch]


# --- Multiplexed WS protocol (#7 P3) ------------------------------------------
# Structured envelope: one socket multiplexes many topics. Client sends `control`
# frames (subscribe/unsubscribe/ping); server sends `event` frames routed by topic.
# The legacy flat messages above remain the `payload.data` shapes (BarDTO/FillDTO).



class Topic(BaseModel):
    """Structured topic. `channel` + channel-specific `params` (e.g. {symbol})."""

    channel: str  # positions | bars | fills | orders | account | risk | quotes | prices | vwaps | today_ranges | fundamentals
    params: dict[str, str] = Field(default_factory=dict)

    def key(self) -> str:
        """Canonical key = channel + sorted params — the client's refcount/cache identity."""
        if not self.params:
            return self.channel
        joined = "&".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.channel}:{joined}"


class ControlFrame(BaseModel):
    """Client → server control frame."""

    type: Literal["control"]
    op: Literal["subscribe", "unsubscribe", "ping"]
    topic: Topic | None = None


class EventFrame(BaseModel):
    """Server → client event envelope. `payload` carries `{frame_type, data}` for data/status,
    or the ack/error body for control_ack/error."""

    type: Literal["event"] = "event"
    event: Literal["data", "status", "error", "control_ack", "heartbeat"]
    topic: Topic | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
