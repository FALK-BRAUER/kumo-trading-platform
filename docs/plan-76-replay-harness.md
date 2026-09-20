# #76 — Projection replay harness (executable design spec)

> Locked from codex design-check (source-anchored: `backtest/engine.pyx:1178`, `execution/engine.pyx:1561`,
> `cache/cache.pyx:1280`) + the ADR (#68/#72). Pins the Nautilus 1.229.0 invariants #73's projection leans on,
> so a version bump can't silently break the trade spine.

## Scope split (decided)

The sequences in the #76 ticket split across **two harnesses** by data path — both assert **native Nautilus
cache/portfolio state, never a parallel ledger**:

| Harness | Path | Sequences |
|---|---|---|
| **Backtest** (`test_projection_replay.py`) | market data → simulated venue → native `OrderFilled` → position/snapshot/portfolio | netting-id · close→reopen snapshot · cancel/replace · long→flat→short · two-strategy net-sum |
| **Live-reconciliation** (later, with #74) | mocked broker reports through `LiveExecutionEngine` reconciliation | external order · corrections/busts · **restart** |

`engine.reset()` is **not** restart. Faithful restart = real process boundary: process A writes Redis cache
(`flush_on_start=False`, stable `trader_id`, `snapshot_orders/positions`) → exit → process B rebuilds + projects;
OR mock broker reports (#75) through reconciliation. Replaying the same data from scratch is a *determinism*
test, not restart. → restart lives in the live-reconciliation harness / #74.

## Backtest harness mechanics

- **ONE module-scoped `BacktestEngine`** (native globals segfault on a 2nd engine per interpreter — documented in
  `test_app.py`). NETTING venue, instrument `AAPL.XNAS`, driver strategy `MANUAL-001` added once.
- Between sequences: `engine.reset(); engine.clear_data(); engine.add_data(seq_data)` (reset keeps loaded data +
  instruments + strategy; `drop_instruments_on_reset=False` default — preserve). Driver strategy implements
  `on_reset()` to clear its own command buffer.
- **Post-reset smoke assert**: no positions, no position snapshots, no orders — proves no cross-sequence bleed.
- **Feed market data** (quotes) to elicit fills; the driver strategy runs a scripted action per tick. Do NOT
  inject `FillReport`/`OrderStatusReport` here — that's the live path, not BacktestEngine.
- **Isolation is mandatory**: separate `pytest` invocation (marker `engine`), NOT alongside `test_app` in one
  interpreter. A marker alone isn't isolation — CI runs two processes. Canonical gate:
  **`backend/scripts/run-tests.sh`** (default suite + `-m engine` harness, either failing fails the run).

## Invariants to pin

1. `position_id == PositionId(f"{instrument_id}-{strategy_id}")` (NETTING).
2. Snapshot-on-reopen: close to flat then reopen same id → prior leg archived to
   `cache.position_snapshots(pos_id)`, new leg's `realized_pnl` resets (0 / commission-only).
3. Account-net reconciliation SUMS across strategies (two-strategy test).

## First golden test — snapshot-on-reopen (deterministic)

NETTING venue · `MANUAL-001` · `AAPL.XNAS`:

1. Buy 100 @ 100 → open `AAPL.XNAS-MANUAL-001`, realized 0.
2. Sell 40 @ 110 → partial close, same open position.
3. Sell 60 @ 110 → flat/closed, same id, realized on old leg.
4. Submit passive buy limit → **ARMED**: assert no open position, open order exists.
5. Feed crossing data / refill → first fill **reopens same id**.
6. Assert: `cache.position_snapshots(PositionId("AAPL.XNAS-MANUAL-001"))` gained one closed leg; current
   position under same id has new-leg realized 0 / commission-only.

## Build order

1. Harness scaffolding: module-scoped engine fixture + driver strategy (`on_reset`, scripted submit) + reset
   helper + post-reset smoke assert + `engine` marker (pyproject) + two-process CI note.
2. Golden test 1: snapshot-on-reopen (above).
3. Then: cancel/replace · long→flat→short · two-strategy net-sum.
4. Live-reconciliation harness (external/corrections/restart) → folds into #74.
