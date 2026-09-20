# #73 — TradeCycleProjection v0 (executable design spec)

> Locked from codex design-check + Perplexity (both agree: **no native Nautilus trade-cycle primitive** — it's
> an app-level projection over positions + snapshots + orders). `Position.ts_opened` is deterministic and
> identical live-vs-replay. Pinned by the #76 harness. ADR: docs/adr/0001.

## What it is
The thin threading layer over NATIVE state (not a parallel ledger). Round-1 = MANUAL strategy, one cycle per
symbol. Groups native positions + position snapshots + working entry orders into a **TradeDTO** per cycle.

## Architecture — event fold, NOT a stateless cache scan
A per-tick cache scan can render current state but CANNOT reconstruct whether a historical flat gap was bridged
by a working re-entry order → cycle boundaries need a **deterministic fold** maintaining an active-cycle map
keyed by the canonical tuple `(account_id, client_id, instrument_id, strategy_id)`. In-memory for v0; durable
persistence + cold-restart reconstruction = #74. The snapshot cadence MATERIALIZES the fold; it must not assign
boundaries. `ui:stream`/`ui:state` are display transport only, never the reconstruction source.

Lives in a dedicated `TradeCycleProjection` object OWNED/called by `UiFeedStrategy` (which already has the MANUAL
identity, position/order handlers, cache access, publish path). Separate Actor deferred to multi-strategy.

## cycle_id
Opaque string, seeded at the FIRST fill of a flat→open transition:
`{account_id}:{client_id}:{instrument_id}:{strategy_id}:{first_leg_ts_opened_ns}` (+ same-ts ordinal if ever
needed). **Treat as opaque — never parse by delimiter** (instrument/strategy ids contain `.`/`-`). NOT the
native position_id (reused across cycles), NOT the Nautilus TradeId (a fill id). `first_leg_ts_opened` = the
first leg's `ts_opened`, NOT the current leg's (which resets on re-entry).

## State machine (v0)
- **HELD** — open position, qty ≠ 0.
- **ARMED** — flat + a live ENTRY/re-entry working order (`not order.is_reduce_only`). Only entry orders bridge;
  protective/reduce-only exits do NOT keep a flat cycle alive.
- **CLOSED** — flat + no live entry order → cycle ends, drops to history; next open mints a NEW cycle.
- **WATCH** — flat, no trigger, cycle still alive. **Unreachable in MANUAL v0** (no manager/intent signal).
  Structurally in the enum; reachable once managers own intent while flat. Do NOT derive from watchlist/UI/Redis.

Continuation: a reopen is the SAME cycle iff the prior cycle was still ARMED across the flat; if it had reached
CLOSED, the next open is a NEW cycle.

## P&L (native, verified by #76)
Cycle realized = Σ(snapshot legs' realized_pnl) + current leg's realized_pnl. Never a parallel ledger.

## TradeDTO (api/models.py) — ADR identity, not just display fields
`account_id, client_id, instrument_id, strategy_id, cycle_id, manager_id: str|None, state, side (LONG/SHORT/
FLAT), quantity, avg_px_open: float|None (current leg; null while flat), realized_pnl: str (cycle total Money),
leg_count, opened_ts, closed_ts: int|None (cycle end, not just last position close), last_event_ts,
working_orders: list[WorkingOrderDTO]`. Do NOT embed PositionDTO or expose native position_id as identity.
`WorkingOrderDTO`: `client_order_id, side, order_type, quantity, leaves_qty, price, trigger_price,
time_in_force, status, ts_last`.

**Drift fix:** `client_id` = the EXECUTION client (ALPACA/IB), NOT the data client currently passed to
UiFeedStrategy. Thread the exec client id in.

## Publish
`ui:state:trades` — a TTL'd latest-state plane like `positions`, emitted on projection changes + the snapshot
cadence. (A kind can't be both a state key and a normal `ui:stream` frame via the existing `_publish`/
`STATE_KINDS`; stream immediacy, if wanted later, uses a separate `trade` delta kind.)

## Build order
1. `WorkingOrderDTO` + `TradeDTO` in models.py.
2. `api/trade_cycle.py` — `TradeCycleProjection` (fold + cycle_id + state) + pure-logic unit tests.
3. Wire into `UiFeedStrategy` (own it, feed position/order events, thread exec client id) + publish `ui:state:trades`.
4. Prove via the #76 replay harness: run the projection over the golden sequences → assert cycle_id stable
   across the reopen, state HELD→ARMED→HELD (reopen), cycle realized = Σ legs.
5. codex review loop.

## v0 NOT in scope (flags, tests-later)
N cycles per symbol per strategy · opposing-strategy · full Σ==net enforcement · MOMENTUM/ETF_AUTO · managers ·
WATCH · durable/restart reconstruction (#74).
