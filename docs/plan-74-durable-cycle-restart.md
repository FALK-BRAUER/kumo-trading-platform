# #74 — Durable cycle metadata + restart (executable design spec)

> Locked from codex design-check + Perplexity + Nautilus 1.229.0 source reading. The cycle ENVELOPE is domain
> state Nautilus doesn't own; it must survive restart and the engine must rebuild live TradeDTO state
> IDENTICALLY live vs restart. Parent #68, ADR docs/adr/0001. Builds on #73.

## The core problem (source-confirmed)
`cache.position_snapshots(pos_id)` — the close-reopen leg archive #73 sums for cycle P&L — is an **in-memory
dict** (`cache.pyx:142`). Nautilus `load_cache` restores positions + orders, NOT that archive. So after
restart, cycle P&L would drop every pre-restart leg.

BUT with `snapshot_positions=True`, Nautilus persists each closed leg's full `Position.to_dict()` (incl.
`realized_pnl`, `ts_opened/closed`) to `snapshots:positions:{pos_id}` on close (`execution/engine.pyx:1814`,
`open_only=False`) — it just never auto-reloads them. **The gap-fill reads Nautilus's OWN persisted state**;
no parallel ledger, no fill-recompute.

## Native persistence (config)
`_durable_configs()` in engine_node, gated by `KUMO_DURABLE_CACHE` (default OFF — paper stack unaffected):
`CacheConfig(database=DatabaseConfig(type='redis', host=$KUMO_REDIS_HOST), encoding='msgpack',
flush_on_start=False, use_trader_prefix=True, use_instance_id=False)` +
`LiveExecEngineConfig(load_cache=True, snapshot_orders/positions=True, reconciliation=True,
reconciliation_lookback_mins=10080, filter_unclaimed_external_orders=True, generate_missing_orders=False)`.
Same Redis server as the ui:stream bus (Nautilus namespaces `trader-{id}:*`, bus is `ui:*` — no collision);
AOF/Redis-own-restart durability is a later prod note (a node restart ≠ a Redis restart).

## Gap-fill (projection) — DONE this increment
`CycleLeg` (ts_opened, ts_closed, realized_pnl) — a common leg shape from EITHER a live `Position`
(`from_position`) OR a persisted state-dict (`from_state_dict`). The projection's cycle P&L consumes CycleLegs,
merging live snapshots with restored legs (dedup by ts_opened). Restart-seed API on the projection:
- `seed_cycle(account, instrument, cycle_id, opened_ts)` — restores the cycle identity + open anchor (the
  boundary native state can't express) so the SAME reducer resumes on the SAME cycle_id.
- `seed_restored_legs(pos_id, legs)` — restores the closed legs' realized.
Proven by `test_gap_fill_recovers_cycle_pnl_across_restart`: post-restart stub cache (position restored,
snapshots gone) → cycle realized recovers to `1000.00 USD` (would be `0.00` without the fill).

## Remaining (next increments)
- **74b — Postgres envelope**: engine-owned table (cycle_id, canonical key, open_anchor_ts, opened/closed ts,
  order/leg bindings, close_reason [default `manual_walk_away` v0], import_status). Engine writes on lifecycle
  transitions (idempotent upserts); reads on startup → `seed_cycle`. Alembic migration. Document the Postgres
  README distinction (engine-owned domain metadata, NOT native trade state or a P&L ledger).
- **74b — Redis snapshot reader**: read `snapshots:positions:{pos_id}` keys on startup, deserialize → CycleLegs
  → `seed_restored_legs`. Needs the durable node to have persisted them (integration).
- **74c — live-node equality test**: real process boundary (Process A writes → kill → Process B reloads), assert
  identical TradeDTO (identity/state/boundaries/bindings/P&L). Plus close_reason/import_status. `needs_services`.
- Then the #76-deferred live-reconciliation sequences (external order, corrections/busts) through
  `LiveExecEngine` reconciliation.

## 74b.2 / 74c — the integration finish (build order: HARNESS FIRST)

Don't write the wiring ahead of its verification — a live durable node is the enabler for BOTH.

**Redis leg-reader (findings, source-confirmed):** Nautilus persists closed-leg states under
`{trader_key}:{general_prefix}:snapshots:positions:{pos_id}` (`cache/database.pyx:280-282` strips two `:`
prefixes on read). So the reader **scans by suffix** (`*snapshots:positions:*`) — no need to hardcode the
prefix. Value = the serializer's bytes of `position.to_dict()` (encoding=`msgpack`). Filter to CLOSED legs via
`CycleLeg.is_closed_state` → `seed_restored_legs`. **The exact wire (serializer framing + prefix) MUST be
verified against a running durable node before trusting the reader** — that's the spike below.

**Envelope wiring (Postgres, no key-format dependency):** engine constructs `CycleEnvelopeStore` when durable +
Postgres reachable; `on_start` schedules `load_active()`→`seed_cycle` on the node loop (gate publishing on a
`_cycles_seeded` flag so nothing mints before the seed); `_publish_trades` schedules
`upsert(envelope_from_dto(dto))` per DTO via `run_coroutine_threadsafe(..., self._loop)` (mirror the watchlist
reconcile). All behind try/except — a DB hiccup must never crash the trading loop. Engine service needs
`KUMO_DATABASE_URL` in compose.

**74c harness (the verification + enabler):** a live `TradingNode` with durable Redis cache + a **sandbox exec**
(`nautilus_trader.adapters.sandbox`, offline fills off fed market data) + Postgres. Process A: open→partial→
flat→ARMED→refill, stop while ARMED, assert TradeDTO `C` + envelope row + redis snapshot keys (discover the
exact format here). Kill A. Process B: same `trader_id`/Redis/Pg, `flush_on_start=False` → load_cache →
reconciliation → read envelope (`seed_cycle`) + read redis snapshots (`seed_restored_legs`) → assert IDENTICAL
TradeDTO (identity/state/boundary/P&L). `needs_services`. This is a multi-hour integration build — start fresh.

## Known v0 limitations (codex-reviewed, accepted)
- **Seed-window race:** `_publish_trades` no-ops until `_cycles_seeded`, so during the (ms) async seed a
  live close→reopen WITHOUT a bridging entry could be missed by the reducer and wrongly continue the seeded
  cycle. Negligible for MANUAL (human-paced, no fills at the startup instant) + durable is off by default. The
  robust fix is an atomic pre-event seed (blocked by on_start running on the loop thread — can't await there);
  tracked for when auto strategies land.
- **Snapshot-scan cost:** `read_restored_legs` SCANs + full-LRANGEs every position-snapshot list on startup.
  Correctness-safe (the projection filters by `opened_ts`), but the lists grow over a long-lived node's life.
  Future: load active envelopes first, then read only those `pos_id`s. Fine at the v0 universe size.
- **Engine wiring fidelity:** `seed_projection` (the seed glue) is unit-tested with fakes
  (test_cycle_restart); the full `UiFeedStrategy` restart on a live/sandbox TradingNode (reconciliation timing,
  the loop scheduling) is not yet integration-tested — the algorithm is proven end-to-end against real Redis in
  test_restart_equality.

## Guardrails
- Same reducer live vs restart — the only restart-only op is seeding durable fold state. No "scan and guess".
- P&L stays native (Nautilus's realized, read from its persisted state). Envelope = boundaries + bindings only.
- All gates default OFF. Never place an order (paper or live).
