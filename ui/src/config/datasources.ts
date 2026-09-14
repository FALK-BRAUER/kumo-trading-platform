/**
 * DataSource composition root — registers the named sources tiles bind to. Imported for side effect
 * by the board (alongside `./tiles`). Keep source→endpoint/topic wiring here, not in tiles: a tile
 * declares the source *name*, this file maps it to a transport.
 *
 * `positions` is REST today; it becomes `hybrid` (REST bootstrap + WS freshen) in #7 phase 3 by
 * changing only this entry — no tile change, since tiles never see `endpoint`/`topic`.
 */
import { registerSource } from "@/lib/framework/datasource/registry";

registerSource({
  name: "positions",
  kind: "rest",
  endpoint: "/positions",
});

// Trade-cycle plane (#73/#77): per-strategy trade CYCLES (HELD/ARMED/WATCH/CLOSED, cycle P&L, dual-lens flags),
// one shared sub, re-pushed each tick. Topic = `trades`. The managed Portfolio tile (#77) reads this instead of
// the flat `positions` list; qty-0 ARMED cycles stay visible.
// Strategy plane (#212): lifecycle, the latest decision with its per-symbol REASONS, journal rows and
// the exit trail. Everything else here describes the ACCOUNT; this is the only source that says WHY.
// One shared subscription, republished by the engine every 20s.
registerSource({
  name: "session",
  kind: "ws",
  channel: "session",
});

// Account equity curve (#243): the BROKER's own history per period (1W/1M/3M/all), not anything derived
// here. Republished every ~2 minutes — a curve is not tick data. Also carries each period's `pnl`
// straight from Alpaca, which is #233's account-level day/week/month figure.
registerSource({
  name: "equity_curve",
  kind: "ws",
  channel: "equity_curve",
});

registerSource({
  name: "trades",
  kind: "ws",
  channel: "trades",
});

// Manager plane (#402 second half). REST, not WS: the engine publishes no `managers` channel — the rows
// live in Postgres and are read back, because the ack for the original attach command expires long
// before an overnight manager fires.
//
// The trades plane cannot answer this on its own. `manager_id` is NULL on the cycle even when a manager
// row exists (verified on CRAK, 2026-08-21), so "1 armed" could never be turned into a noun without
// this binding.
//
// 20s: a manager changes state on a fill or a trigger, not on a tick, and this is a Postgres read
// behind the API rather than a market-data subscription.
registerSource({
  name: "managers",
  kind: "rest",
  endpoint: "/managers",
  cacheMs: 20_000,
});

// Quarantine plane (#79): broker activity the cockpit didn't originate (EXTERNAL / foreign strategy), one
// shared sub, re-pushed each tick. Topic = `external_activity`. Surfaced separately so it never joins strategy
// P&L; feeds a future broker-net discrepancy indicator on the managed Portfolio (#77).
registerSource({
  name: "external_activity",
  kind: "ws",
  channel: "external_activity",
});

// Per-symbol bar stream over WS. Bound with `{ symbol, granularity }`; topic = `bars:granularity=<g>&symbol=<s>`.
// granularity (candle) drives the multi-timeframe chart (#58) — the backend serves per-granularity and streams
// live increments. Defaults to 1d so callers that don't pick a candle (price fallback) keep the daily series.
registerSource<{ symbol: string; granularity?: string }>({
  name: "bars",
  kind: "ws",
  channel: "bars",
  params: (cfg) => ({ symbol: cfg.symbol, granularity: cfg.granularity ?? "1d" }),
});

// All fills over WS (snapshot-only today; one shared, ref-counted sub across all rows). Topic = `fills`.
// Consumers filter to their instrument client-side — chart entry/exit markers come from here.
registerSource({
  name: "fills",
  kind: "ws",
  channel: "fills",
});

// Orders blotter (#33): working + recent-terminal orders, one shared sub, re-pushed each tick. Topic =
// `orders`. Working orders sort ahead of terminal. Cancel/Modify are REST actions (client.ts), not here.
registerSource({
  name: "orders",
  kind: "ws",
  channel: "orders",
});

// Live-price plane (#28): real-time last trade for every streamed symbol, one shared sub. Topic = `prices`.
// Decoupled from `bars` (which drives chart candles) — consumers read their symbol's live price from here.
registerSource({
  name: "prices",
  kind: "ws",
  channel: "prices",
});

// NBBO quote plane (#40): real-time bid/ask for every streamed symbol, one shared sub. Topic = `quotes`.
// Feeds the order ticket's spread/mid/marketable-limit prefill + the auto-select rules. Read via useInstrument.
registerSource({
  name: "quotes",
  kind: "ws",
  channel: "quotes",
});

// Session VWAP plane (#182 follow-up, watchlist KPI Phase 2): RTH-only, ET-session-anchored, one shared
// sub for every streamed symbol. Topic = `vwaps`. Absent for a symbol until real volume has accumulated
// this session (backend: `UiFeedStrategy._update_vwap_from_bar`). Read via useInstrument.
registerSource({
  name: "vwaps",
  kind: "ws",
  channel: "vwaps",
});

// Today's Range + Prior Close plane (#182 follow-up, watchlist KPI Phase 3): Alpaca batch snapshot,
// refreshed on the watchlist timer's ~5s cadence, one shared sub for every streamed symbol. Topic =
// `today_ranges`. Absent for a symbol until the engine's first successful batch fetch. Read via useInstrument.
registerSource({
  name: "today_ranges",
  kind: "ws",
  channel: "today_ranges",
});

// Fundamentals plane (#182 follow-up, watchlist KPI Phase 2): Financial Modeling Prep, refreshed on its
// own low-frequency (~6h) timer, one shared sub for every streamed symbol. Topic = `fundamentals`. Absent
// for a symbol until the engine's first successful fetch, then persists through transient failures (no
// tombstone — see engine_node.py's `_refresh_fundamentals`). Read via useInstrument.
registerSource({
  name: "fundamentals",
  kind: "ws",
  channel: "fundamentals",
});

// Account plane (#41): equity / cash / buying_power / multiplier, one shared sub. Topic = `account`.
// Feeds %-of-equity sizing + affordability in the order ticket. Read via useAccount().
registerSource({
  name: "account",
  kind: "ws",
  channel: "account",
});

// Market rotation (#351, #384): the Ichimoku read over ETF ratio axes, computed BY THE ENGINE off our
// own bars and re-published on its timer.
//
// WS, LIKE EVERY OTHER FACT THE UI READS. Operator, 2026-08-21: "It should work similar to trend lines,
// watch, portfolio etc." It was `rest` because the payload used to be a file written by a sidecar —
// there was no publisher to subscribe to, so the tile polled the api which polled the disk. Both of
// those are gone: the engine publishes `rotation` on the bus exactly as it publishes trades, session,
// account and equity_curve, and this subscribes to it.
//
// The difference is not tidiness. A poll asks "is there anything new" on a fixed clock and cannot tell
// a fresh answer from a stale one; a plane pushes when the fact changes and stops arriving when the
// producer stops — which is the signal that was missing when a dead sidecar served a four-hour-old
// payload that looked perfectly healthy.
registerSource({
  name: "rotation",
  kind: "ws",
  channel: "rotation",
});
