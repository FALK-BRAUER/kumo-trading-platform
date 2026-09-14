# Scope — #28: live prices frozen (Alpaca live-bar aggregation + reconnect resilience)

## Symptom
Market open → watchlist + detail chart show no live price movement. Prices frozen at last historical close.

## FINALIZED SCOPE (Codex review, 2026-07-07) — smaller than first drafted

Codex correction (BLOCKER): the UI does **not** read 1-HOUR. The `bars` source sends only `{symbol}` →
FastAPI defaults granularity to **`1d`** (`app.py:49`); watchlist/portfolio/detail read the last **daily**
bar's close. The engine subscribes 1m/1h/1d (all EXTERNAL, `engine_node.py:61/138`, `bar_spec.py:15`);
Alpaca streams 1m + daily, only 1h is unsupported — a *separate* broken sub the UI never consumes.

**Actual frozen cause = reconnect (Gap 2):** the WS died in the DNS blip and the bounded 5-attempt initial
connect gave up → no frames of ANY granularity → frozen. Engine restart already revived it.

**v1 fix (do now):**
1. **Reconnect resilience.** `_connect()` must not abort on bounded attempts. Start WS connect+reconnect in
   the **background** (do NOT block kernel startup — it caps at ~60s for connected engines), retry forever
   with exponential backoff + jitter, mark the client degraded/disconnected and surface it (→ #26). The
   post-first-connect `_run` loop is already unbounded — reuse it.
2. **Live price → 1-minute.** Switch the UI live-price plane off daily: the `bars` datasource requests
   `granularity=1m` (backend already stores/serves by `(symbol, granularity)` — `consumer.py:40`,
   `app.py:53`; WS appends increments — `ws-manager.ts:201`). Keep `BarDTO`. Daily-close as a live price is
   too coarse for a live-trading cockpit.
3. **Drop the broken 1-HOUR EXTERNAL live subscription.** Chart hourly/daily candles come from REST history
   (already loaded via `lookback_days`). No live hourly needed.

**Deferred to v2 (over-scoped for the freeze fix):**
- `TradeTick` last-price plane — truer for sparse IEX, but Alpaca adapter has no `_subscribe_trade_ticks`
  and drops trade/quote frames (`data_client.py:203`); needs parse + subscribe + DTO/topic + UI contract.
- Nautilus INTERNAL composite live chart aggregation — needs `bar_spec` composite strings +
  `request_aggregated_bars` (not `request_bars`). Keep chart candles on REST external for now.
- Shutdown `TypeError` (Gap 3) — cosmetic.

---

## (original) Reframe (Operator, 2026-07-07): hourly bars as a LIVE feed is the design bug
An hourly bar only produces a value when the hour closes — ~7 updates/day. Subscribing it "live" can never
give a live price, for ANY vendor. So the real fix is not "stream/aggregate hourly live" — it's:
- **Live plane = trade ticks (or 1-minute at coarsest)** — continuous price for the watchlist last-price and
  any live-trading decision. This is what should be subscribed live.
- **Hourly / daily bars = chart context only** (Ichimoku TA timeframes), built by aggregating the tick/1-min
  stream (Nautilus INTERNAL) or from REST history — NEVER streamed live as hourly.
The current 1-HOUR live subscription for the watchlist price is **deleted**, not fixed.

## Root causes (grounded in code)

### Gap 1 — no live bars for the subscribed aggregation (PRIMARY, the frozen-price cause)
`AlpacaDataClient._subscribe_bars` (`data_client.py:181`) maps only:
- `BarAggregation.MINUTE → "bars"`, `BarAggregation.DAY → "dailyBars"`.
`HOUR` (and anything else) → `channel is None` → logs `aggregation unsupported` and **returns without subscribing**.
The cockpit subscribes **1-HOUR** live bars (`AAPL.XNAS-1-HOUR-LAST-EXTERNAL`) → no live channel → nothing streams. Alpaca's live MD WS only emits 1-minute (`b`) and daily (`d`) bars (+ trades/quotes); it does **not** emit hourly.

**Fix (per the reframe):**
- **Live price → TradeTick (primary) / 1-minute bars.** Subscribe Alpaca trades (or 1-min bars) for the
  live plane; the watchlist/portfolio last-price reads this. Alpaca DOES stream both. For sparse IEX names,
  TradeTick is truer than 1-min bars (show "no recent trade" rather than a stale minute-close).
- **Chart candles (1h/1d) → Nautilus INTERNAL composite** aggregated from the live 1-min (or REST history).
  `_subscribe_bars`: EXTERNAL-unsupported-granularity (1-HOUR EXTERNAL) → still reject; but a composite
  INTERNAL 1-HOUR request → ensure the base 1-min EXTERNAL subscription and let Nautilus' `BarAggregator`
  build the hourly. No hand-rolled roll-up.
- **Delete** the current 1-HOUR-as-live-price subscription (the watchlist row's live feed).

Decision to confirm with Codex: exact subscription-site changes (does the UI/engine request EXTERNAL 1-HOUR
today? switching the live plane to trades/1-min ripples into the WS topic granularity + REST backfill).

### Gap 2 — startup connect gives up permanently (resilience)
`AlpacaWebSocket.start(max_attempts=5)` (`websocket.py:59`) retries the initial connect 5× then **raises** →
the data client aborts. If the engine starts during a transient DNS/network blip (as it did — recreated
while docker DNS was flaky), live data **never** comes up until a manual engine restart. The `_run` reconnect
loop (post-connect drops) is unbounded and fine; only the **initial** connect is fatally bounded.

**Fix:** don't permanently abort — keep retrying the initial connect in the background with backoff+jitter
(or start the read/reconnect loop regardless and let it establish the connection), and surface the
disconnected state to the UI (ties to #26 — silent stale data is the worst outcome for a trader).

### Gap 3 — shutdown error (minor)
On engine stop: `TypeError("'Event' object is not callable")` in `TradingNode.stop_async` →
`Trader: Error on STOP`. Cosmetic on shutdown; scope a look but low priority.

## Open questions (Perplexity scoping + Codex review)
1. Nautilus idiom: for a venue that only streams 1-min bars, is INTERNAL bar aggregation (DataEngine builds
   1h/1d from the 1-min feed) the standard path? What exactly must the client advertise/handle for a
   composite `SubscribeBars`, and what must the UI change (bar-type aggregation source)?
2. For a *live last price* specifically (watchlist tile), is subscribing 1-min bars enough, or should we use
   trades/quotes (finer, truer last price) and reserve bars for the candle chart?
3. Reconnect: best-practice unbounded backoff-with-jitter for a market-data WS; when to give up vs keep
   trying; how to expose "disconnected/stale" so #26 can render it.
4. Any Alpaca IEX-feed caveat (thin free feed) that changes the recommendation (e.g. sparse 1-min bars for
   illiquid names → last price still looks stale)?

## Out of scope
- The #26 UI work itself (surfacing the state) — separate. Multi-granularity chart backfill changes.
