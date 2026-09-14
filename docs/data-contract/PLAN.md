# Plan — demand-driven market data + a capability contract (#618)

Baseline tagged `v0.0.0` = `1a94d97`. Every step below is independently shippable and independently
revertable. Each goes through the 11-step defect procedure in CLAUDE.md.

## The problem, measured

`engine_node.py:658` — `self._granularities = list(cfg.chart_lookback_days)`. The entire venue
subscription surface is derived from a config table named **chart**, at boot, as a full cross-product
of symbols x granularities, held forever whether or not anything asks:

```
STRATEGIES   200 subscriptions   1-DAY-EXTERNAL              <- the only ones that place orders
UI PLANE     431 bar + 56 tick   1m/5m/15m/30m/1h/1d/1w      <- display
```

575 of 631 bar subscriptions and all 56 tick subscriptions serve display. On IBKR they saturate
pacing and starve the daily bars the strategies depend on (744-request bursts, #617).

Three consequences already cost us:

1. `[chart.lookback_days]."1w" = 1825` is justified in-file as "also the 5Y watchlist trend window".
   **Stale since #612** moved 5Y to `d1`. 1825 days x 115 symbols now serve one selector button.
2. `[chart.defaults]."1W" = "1h"` — `1h` is INTERNAL, aggregated from trade ticks IBKR does not
   deliver. #612 fixed the trend strip's 1W; the CHART's 1W range is still structurally dead there.
   Same defect, second location, not covered by the fix.
3. `"1d" = 300` is justified by MA200 — a WATCHLIST column. Tune the chart, move the strategies.

## What already exists (do not rebuild)

The framework seam is correct and largely built:

- `datasources.ts` registers 15 named sources; tiles bind by NAME and never see transport. The file
  states the property for `positions`: REST -> hybrid "by changing only this entry — no tile change".
- `bars` is ALREADY parameterized: topic `bars:granularity=<g>&symbol=<s>`.
- `app.py::_snapshot_for` dispatches on `topic.channel` + `topic.params`.
- `_after_definition` is a single loop over `self._granularities` with ZERO venue knowledge in it.

**The gap is one wire.** Demand stops at the WS layer: a client subscribing to
`bars:granularity=5m&symbol=OKTA` tells `app.py` exactly what it wants and nothing carries that to the
engine. Topics are demand-driven; upstream subscriptions are not. Both ends exist, unconnected.

This is why this is NOT "rewrite parts of the app". No tile changes in steps 1-5.

## Architecture

Three layers. Only layer 3 is new code of any size.

```
1. CAPABILITY   adapter declares, per granularity:  live | historical | none
2. REQUIREMENT  consumer declares:  need(granularity, max_staleness, criticality, scope)
3. RESOLVER     union the needs -> order by criticality -> ask the adapter
                -> answer each need: served | degraded | unavailable
```

- `unavailable` is a first-class value the UI RENDERS, not a blank that looks like a bug.
- Criticality is what stops a chart zoom starving a strategy's daily bars.
- No venue name above the connector. A third adapter declares what it serves; the platform degrades
  predictably instead of every consumer learning venue trivia.

### The requirement table (derived by tracing consumers; exists nowhere in the repo today)

| consumer | needs | scope | staleness | if missing |
|---|---|---|---|---|
| BCTROT / MOMENTUM / QC345 / TECHIVOL | `1d` | universe (~100) | prior close | **CANNOT TRADE** |
| Market compass | `1d`, ~3y | 27 reference | daily | grade on covered (#606) |
| Trend strip (all 6 labels) | `1m`, `1d` | every visible | min / day | mark uncovered |
| Watchlist MA200 / Blue Flag | `1d`, >=200 trading bars | watchlist | daily | blank column |
| Watchlist Ichimoku SIG | `15m` | watchlist | minutes | blank column |
| Live price (header, ticket, marks) | trade tick, else `1m` | every visible | seconds | **NO PRICE** |
| Chart | ONE of `1m 5m 15m 30m 1h 1d 1w` | one symbol | minutes | blank that zoom |
| Order ticket mid / marketable limit | quote ticks | one symbol | seconds | fall back + warn |
| Protection / ATR ratchet | intraday | held only | minutes | hold last, report degraded |
| Feed-alive | anything | — | seconds | INERT (#613) |

**Always-on union: `1d` + `1m`, plus `15m` for the watchlist column.** Everything else is one-symbol,
while-open. Those are exactly the granularities BOTH venues stream natively.

## Steps

Each is a separate ticket, branch, PR, and procedure run. Ordered so value lands before risk.

### Step 1 — the live price plane must accept a bar as evidence
The precedence already exists in `_last_price_and_ts_for` (trade tick, else bar close). The LIVE plane
does not use it: `_publish("price")` fires only from `on_trade_tick`, so a venue with no trade ticks
publishes no price at all. This is why staging shows NO PRICE on held positions while bars arrive.

- Publish from `on_bar` too, same precedence, freshest wins.
- Carry a SOURCE MARKER (`tick` | `bar`) and the observation time, so a minute-old price renders
  differently from a second-old one. Three states, not two.
- Order ticket treats a `bar`-sourced mid as degraded and says so — filling a market order off a
  stale mark is a different risk from drawing a sparkline with it.
- Alpaca behaviour UNCHANGED: ticks still win where present. Precedence, not replacement.

Fixes IBKR portfolio/watchlist price and the 1H trendline. No subscription changes. Smallest step,
largest immediate user-visible win.

### Step 2 — refuse the granularity default
`app.py:136` — `granularity = topic.params.get("granularity", "1d")`. A default argument is what makes
a missing argument invisible: a tile that forgets to bind `granularity` silently gets DAILY bars and
renders a plausible, wrong chart. Same family as `_j`'s `slot=` default that killed two lanes.
Refuse the topic instead.

### Step 3 — `data_needs.py`, the declared requirement table
Invert the dependency: `_granularities` derives from DECLARED NEEDS, not from the chart config.

- Pure refactor. Prove it by asserting the derived union EQUALS today's set — no behaviour change.
- Makes the two stale justifications above impossible to leave behind: `1w`'s only declared consumer
  is the chart selector, and the file says so.
- `chart.lookback_days` keeps its history-window role and loses its subscription-deciding role.

### Step 4 — the capability plane
Adapter declares `live | historical | none` per granularity; published as a source like any other.
`_snapshot_for` returns `served | degraded | unavailable` instead of empty-or-None, so "no data yet"
and "this venue will never serve this" stop looking identical. UI grays the zoom rather than blanking.

### Step 5 — demand-driven subscriptions (the -575)
Refcount WS topic -> venue subscription. First subscriber to `(symbol, granularity)` subscribes
upstream; last unsubscribe drops it.

- `1d` and `1m` PINNED always-on, plus the strategy universe's `1d`. A display refcount must never be
  able to unsubscribe a trading input.
- Behind a flag defaulting **False** (CLAUDE.md: all new automation gates default False).
- Must survive reconnect: a resubscribe storm on WS reconnect is the failure mode to test for.
- This is what makes #617's burst go away.

### Step 6 — composite intraday bar types
`bar_spec.py` records rejecting `5-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL` for two reasons, BOTH
display-quality arguments about Alpaca (sub-second forming candle; one canonical bar type). Neither is
a correctness argument, and that choice is why five granularities are structurally dead on IBKR.

A composite over `1-MINUTE-EXTERNAL` streams on BOTH venues. Cost: the forming candle updates once a
minute instead of sub-second — on the CANDLE only; the live price plane is separate (step 1) and keeps
its ticks where they exist.

Risk: bar-type STRINGS change, so cache keys change and history reloads; `_SUFFIX_TO_GRANULARITY` must
round-trip. Do this after step 5, never with it.

### Step 7 — remove the venue names above the connector (#616)
| site | fate |
|---|---|
| `engine_node.py:977` `if data_provider == "alpaca"` -> `AlpacaHttpClient` for Today's Range | **DIES** — today's high/low/prior-close is a `need(1d)` derivable from bars on any venue |
| `engine_node.py:1160` `if data_provider == "databento"` live-edge backoff | **DIES** — becomes a declared capability |
| `engine_node.py:6682` Alpaca `feed=iex\|sip` override | **MOVES DOWN** — legitimately Alpaca-only concept, belongs in the provider module, not `build_node` |

Two removed, one relocated below the seam. After that there is no venue name above the connector.

## Order and why

1, 2 are independent, tiny, and immediately valuable — ship first. 3 is a safe pure refactor that makes
5 tractable. 4 makes 5's failures honest. 5 is the big win and the biggest risk, so it lands behind a
default-False flag with the strategy inputs pinned. 6 and 7 are cleanup that only make sense after the
contract exists.


## AMENDMENT — service tiers, adaptive cadence, and a capability banner (Operator, 2026-08-28 03:07)

Operator: *"api wise you can kind of give higher level options like realtime and other granularities and
you serve as per what the stack offers. maybe even automatically switch to another type of bars when
you need to throttle."* And: *"what is currently showing feed live can show an indicator of which
level can be served at this backend."*

This replaces the "capability per granularity" layer above. It is strictly better, because it puts
MECHANISM SELECTION inside the adapter — which is the actual goal of "no venue name above the
connector". The old shape still leaked venue knowledge upward: a consumer asking for `1m` has already
assumed a bar exists. A consumer asking for `fast` has assumed nothing.

### Consumers ask for a TIER, not a bar type

```
TIER        meaning                     Alpaca serves with      IBKR serves with
realtime    sub-second, per event       trade/quote ticks       tick-by-tick (capped, often refused)
fast        seconds to ~1 min           1m bars (native ws)     1m keepUpToDate
slow        minutes to a session        5m..1h, 1d              same, keepUpToDate
daily       prior close                 1d dailyBars            1d keepUpToDate
```

`need(tier, max_staleness, criticality, scope)`. The adapter answers with what it ACTUALLY bound:

```
resolve(need) -> { tier_requested, tier_served, mechanism, cadence, state }
                 state: served | degraded | unavailable
```

`tier_served < tier_requested` is **degraded**, not failure — a first-class state, reported as itself.
Three states, never two: "asked and got it", "asked and got less", "cannot be served here".

### Adaptive cadence, and throttling as a DEGRADE not a DROP

Cadence is a property of the binding, not a constant. Operator: 5s can become 1 min.

- Each binding carries a current cadence; the adapter may lower it under pressure.
- **When a venue limit bites, the adapter DOWNGRADES THE TIER rather than dropping the need.** IBKR
  refusing tick-by-tick (10189/10190) becomes `realtime -> fast, mechanism=1m keepUpToDate,
  state=degraded` — instead of today's behaviour, which is nothing at all and a blank UI.
- Downgrade is announced, never silent. A silent downgrade is the fallback pattern CLAUDE.md forbids:
  it would be indistinguishable from a healthy feed.
- Criticality bounds it: a strategy's `daily` need may never be downgraded. Display needs may.

### The banner becomes the capability indicator

`● feed live` today is binary and says nothing about WHAT is live. It becomes the visible surface of
the whole contract — the cheapest possible way to make this legible, and it already exists and is
already venue-neutral after #608/#611:

```
● live · realtime      ticks flowing            (Alpaca, entitled)
● live · fast          1m bars, no ticks here   (IBKR today)
● degraded · fast      asked realtime, throttled to 1m
● delayed · slow       history only
○ no feed              INERT (#613)
```

Per-tenant, and per-symbol on hover. **This is what makes "IBKR is a ghost" impossible to miss again**
— tonight's whole investigation was reading a binary banner that could not express the difference
between "live" and "live at a tier you did not ask for".

### Consequences for the steps

- Step 1 (price from bars) is UNCHANGED and still first — it IS the `realtime -> fast` downgrade for
  the price plane, hand-rolled for one case. Build it so its source marker is the tier, not a bool.
- Step 4 becomes: publish the tier/capability plane AND render it in the banner. Bigger than the old
  step 4, and worth more — it is the observability for everything after it.
- Step 5 (refcount) keys on `(symbol, tier)` rather than `(symbol, granularity)`, which also removes
  the step-5/step-6 ordering hazard entirely: composite bar types become an adapter-internal mechanism
  change, invisible to the refcount. **Codex asked whether 6 should precede 5; with tiers the question
  dissolves.**
- Step 6 stops being a UI-visible change and becomes "the IBKR adapter's mechanism for tier `slow`".

## Deploy windows tonight (SGT)

Market open until 04:00. Paper BCTROT close-20m fires 03:40; no deploy 03:00-03:35. After 04:00 free.
Code and tests any time; engine touches only in a window.

## Rollback

`v0.0.0` = `1a94d97`. Steps 1-4 are additive and revert cleanly. Step 5's flag defaults False, so
reverting is flipping it. Step 6 forces a history reload on revert — note it in the PR.

## Explicitly NOT in scope

- Restoring the open+300m/open+315m slot timings (kumo-strategies says not backtested; the operator decides).
- #617's burst as a separate fix — step 5 subsumes it; if step 5 slips, #617 gets its own throttle.
- The `report.filled_qty 80` mystery (deferred by the operator).
- Anything touching order submission.
