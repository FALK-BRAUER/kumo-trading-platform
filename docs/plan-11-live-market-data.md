# Plan — #11 Live market-data via a provider-agnostic Nautilus data client

> Status: proposed (2026-06-29). Supersedes the parked "historical-into-BacktestEngine" A slice.
> Goal: a live tape the operator watches to pick trades — real bars at selectable granularity per chart range,
> plus a ~10s price refresh. Data flows through a Nautilus `DataClient` so the provider is swappable by config.

## Decisions locked

- **Architecture = B (native live).** `TradingNode` + Databento **live** data client. Not BacktestEngine.
  Native `request_bars` (historical backfill) + `subscribe_bars` (live) — no custom fetch glue.
- **Provider-agnostic by config.** A global provider registry: `config/feed.toml` names the data/exec
  provider; a registry maps name → Nautilus client factory. Swap provider = one-line edit. Downstream
  (cache, charts, watchlist signal, future lanes) sees only `Bar` — provider-blind.
- **Data and execution chosen independently** (DataClient=Databento, ExecClient=IBKR).
- **Secrets stay in keychain.** Config holds the *env-var name* (`api_key_env`), never the value;
  `scripts/run-api.sh` injects it.
- **Refresh by push, not poll.** Backend emits a price frame ~every 10s over the existing WS; UI renders.
  Honors the no-poll / 429 rule.

## Default range → candle ladder (adjustable per range; native Databento schemas)

| Chart range | Default candle | Databento schema |
|---|---|---|
| ≤ 1D | 1-minute | ohlcv-1m |
| 1W | 1-hour | ohlcv-1h |
| 1M / 3M / 1Y | 1-day | ohlcv-1d |
| 5Y | 1-day (weekly = aggregate later) | ohlcv-1d |

Open: is 1m the right intraday floor, or go finer (1s/trades) for ≤1D? Default assumes 1m.

### Extended hours (decided 2026-06-29)

`XNAS.ITCH` intraday carries the full session (verified: 04:00→20:00 ET, pre + post). Rule, driven by
the discretionary use case (gaps matter live, not in history):

- **Today's session → full extended hours** (pre 04:00 → post 20:00) — the live right edge; pre-market
  gaps/opens are decision-critical.
- **Prior sessions → RTH only** (09:30–16:00) — historical pre/post is noise and muddies the gap anchor.
- **No global toggle.** A client-side, session-aware filter: keep a bar if it's RTH OR belongs to today.
- **Indicators (Ichimoku/MA200) compute on RTH bars** — today's extended candles render but don't distort them.
- **Gap sensitivity** needs only prior RTH close → today's open/pre-market — both present under this rule;
  historical ETH would weaken it. Add a **prior-close reference line** (yesterday's RTH close) as the gap anchor.
- Pure client-side (we already fetch full-session intraday) — no backend change, no refetch on view change.

## Work breakdown

### Backend

1. **`config/feed.toml`** (rebuilt, provider-registry shape) + **`config/README.md`**.
   `[data].provider`, `[data.databento]` (api_key_env, dataset), `[execution].provider`,
   `[universe]` symbols, `[chart.defaults]` range→candle ladder.

2. **`api/feed_config.py`** — typed loader for the above (dataclasses). Validates provider names
   against the registry. `KUMO_FEED_SOURCE`-style override kept for tests (force a stub provider).

3. **`api/providers/__init__.py` (registry)** + **`api/providers/databento.py`** —
   `register(name) -> (data_client_factory, client_config_builder)`. databento entry wires
   `DatabentoLiveDataClientFactory` + `DatabentoDataClientConfig` from config. New provider = new module
   + one registry line (write a Nautilus adapter only if none exists, out-of-tree per no-fork rule).

4. **`api/node.py`** — rebuilt around `TradingNode` (was BacktestEngine):
   - Build `TradingNodeConfig`, `add_data_client_factory(provider)`, `node.build()`, run.
   - **Async integration risk (main one):** `TradingNode.run()` owns its own asyncio loop and blocks —
     conflicts with FastAPI's loop. Mitigation: run the node in a dedicated thread with its own event
     loop; FastAPI reads the Nautilus cache (thread-safe reads) and a thread-safe queue bridges live
     events → WS. Spike this first (see Risks).
   - Strategy (extend `BridgeStrategy` or a new `FeedStrategy`): on start `request_bars` for the
     configured backfill window per granularity, `subscribe_bars` for the live granularity, and
     `subscribe_trade_ticks`/`quote_ticks` for the price refresh. Collect into cache/DTOs.

5. **`api/app.py` WS contract** — `bars` topic gains a `granularity` param (1m|1h|1d). ✅ DONE 2026-06-29.
   On-demand: daily backfilled at start; intraday fetched the first time a granularity is subscribed
   (cached after). Verified over WS: 1h → 454 bars in 7.1s, full extended hours. NB: the on-demand fetch
   is a synchronous Databento request that briefly blocks the loop (~7s) — offload off-loop later. Still
   to add: `range`/lookback param wiring + the live `price` push frame.

   (original scope) — `bars` topic params gain `granularity` + `range`:
   - subscribe → snapshot = the requested (symbol, granularity, range) series (from cache/request_bars).
   - live `bar` frames as bars close; new `price` frame ~every 10s (throttled from trade/quote ticks).
   - On granularity/range change the UI re-subscribes; backend `request_bars` fills the new series.

6. **Tests** — `conftest.py` pins a deterministic **stub provider** (registry entry returning canned
   bars; no network/key). Cover: feed_config parse + provider validation, registry lookup, WS snapshot
   shape per granularity, price-frame throttle. (The A slice had no tests for its new code — close that.)

### Frontend (`ui/src/components/tiles/ChartTile.tsx`)

7. **Split range vs candle.** Today `TIMEFRAMES`/`TF_BARS` only slice a daily series. Replace with:
   a **range** selector + a **candle-type** selector, candle defaulting from the ladder per range,
   user-overridable. Each (range, candle) → a WS subscription with `{symbol, granularity, range}`.

8. **Live updates.** Append/replace the forming candle from live `bar` frames; update the ticker/last
   price from the ~10s `price` frame (the old UI's "refresh bar" behavior).

9. **Watchlist tile** reads the same live price/signal off the shared feed (no separate wiring).

## Risks / spike-first

- **R1 — TradingNode ↔ FastAPI event loops (highest). RESOLVED 2026-06-29 — single-loop embedding.**
  Final pattern: build the node on the main thread (kernel installs signal handlers there) and run it as
  a task on uvicorn's own loop via `run_async()` — NOT a daemon thread. The daemon-thread approach
  (spike) deadlocked under uvicorn: building on the main thread binds uvicorn's running loop, which a
  worker thread can't drive. One loop = no cross-thread cache access (the WS layer reads the cache on the
  same loop the node mutates it). Verified live under uvicorn: 127 backfill bars over WS, positions empty.
  Caveat: the kernel overrides uvicorn's SIGINT/SIGTERM handlers — revisit graceful shutdown later.
- **R1a — Instrument bootstrap depends on market hours (found by the spike). MUST FIX.** The Databento
  *live* instrument provider awaits definitions on the live stream (`Awaiting instrument definitions…`);
  outside RTH nothing streams → 15s timeout → `price_precision` can't resolve → `request_bars` fails.
  Charts must work outside RTH, so **preload instrument definitions from the historical `definition`
  schema at startup** (don't rely on the live definition stream). Also cap `request_bars` `end` inside
  dataset availability (ohlcv-1d ends one day prior) — the spike hit a 422 otherwise.
- **R2 — Market hours.** Live ticks only during US RTH (opens 21:30 SGT). Outside RTH: backfill + last
  close, no live increments. Set UI expectation (a "market closed" state), not a bug.
- **R3 — Real-time data cost.** Live streaming carries pass-through exchange fees (vs ~$0 historical).
  Confirm the live subscription cost for the universe before leaving it on continuously.
- **R4 — Multi-granularity volume.** Subscribing 1m for many symbols is heavier than daily. Start with
  the 2-symbol universe; measure.

## Sequencing

1. ~~Spike R1~~ ✅ done 2026-06-29 — in-process viable; surfaced R1a (instrument bootstrap).
2. **Instrument-definition preload** from the historical `definition` schema (fixes R1a; must land before
   request_bars so precision resolves outside RTH).
3. Provider registry + feed_config + stub provider + tests (no network).
4. node.py on TradingNode + databento live provider; verify backfill works outside RTH, then a live bar
   in-process at the open (≥ 21:30 SGT).
5. WS contract (granularity/range params + price frame).
6. ChartTile range/candle split + live wiring.
7. Watchlist reads shared feed.

## Carried over from the parked A work (already done, reused here)

- Databento key in keychain (`databento-kumo-cockpit`), validated.
- `databento` dep in `pyproject.toml`.
- `scripts/run-api.sh` keychain → env injection; `.cache/` gitignored.
- Provider decision recorded in `CLAUDE.md` + memory.
