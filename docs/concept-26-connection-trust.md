# Concept — #26: connection/health trust (no silent stale, no lying indicators)

## Why it matters
A trader acting on a **stale or dead** price is the worst outcome — worse than seeing nothing. The cockpit
must always make the truth obvious: is the data live, stale, or gone? Proven live this session: `redis`
died → api crash-looped → UI showed "no portfolio" (silent empty) as if the account were flat; and the
Alpaca WS froze → prices stuck with no signal. Both read as "normal" to the user. Unacceptable for trading.

## FRAMEWORK DIRECTIVE (applies here too)
Connection/health/staleness is a **cross-cutting framework concern**, surfaced **uniformly** — ONE app-level
truth + consistent per-tile states. Tiles must NOT each hand-roll "unavailable"/"disconnected" UI (today
PortfolioTile prints its own "positions unavailable"; RefreshBar has its own bar). Two tiles in the same
state look the same, by construction.

## The three truths a trader needs (currently conflated / missing)
1. **API reachable?** — is the FastAPI process up? (REST `/health`.) Today: not checked; a dead api just
   yields failed fetches → per-tile empty.
2. **Live channel connected?** — is the browser WS open? Today: `wsManager.connection`
   (`ws-manager.ts:111`) — but it's **lazy**: only connects when a tile subscribes. An empty view (no WS
   subscriber) shows `disconnected` → RefreshBar red → **the indicator lies** (backend is fine, just no
   subscriber). The state means "is a socket open," not "is the backend reachable."
3. **Data fresh?** — even with the WS connected, is data actually flowing? The engine can be up while the
   Alpaca feed is dead (exactly what happened). No freshness signal today. Need last-frame-received age
   and/or a backend-reported feed-health flag (the `connected` property added to the Alpaca WS in #28 is a
   backend signal we could expose).

## FINALIZED SCOPE (Perplexity + Codex, 2026-07-06) — reshaped by the #20 process split

**Backend — feed-health is a Redis contract, not an api introspection (Codex BLOCKERs):**
- The api process runs `RedisConsumer` (#20) — it has NO access to the engine's Alpaca WS `connected`
  property, and "last `ui:stream` frame age" is meaningless (engine re-pushes position snapshots + api
  re-pushes cached snapshots every 1s → frames arrive even when the market-data feed is dead).
- **Engine publishes explicit `health` frames** to `ui:stream` (periodic): `{feed_connected: bool,
  last_tick_ts per symbol or overall, engine_ok}`. The engine knows the Alpaca WS `connected` (#28) + tracks
  last tick. → `RedisConsumer` tracks the latest health frame + its own bridge state (redis reachable).
- **Extend `GET /health`** beyond process-up (`{"status":"ok"}` today) to the api's OBSERVED state:
  `{api: ok, bridge: connected?, feed: {connected, last_tick_age}}`. It reports what the api observes, never
  pretends to own engine state.

**Frontend — uniform, at the right seams:**
- `useHealth()` — polls `/health` (TanStack Query, jittered ~2–5s) → global banner. Precedence:
  `apiDown` (poll fails) > `feedDegraded` (health frame `feed_connected` false OR last-tick age stale during
  market hours) > `wsIssues` (WS disconnected **only with active subscriptions** — lazy idle WS is neutral).
- **RefreshBar meaning changes**: it must reflect an *active stream transport issue* only (WS down while
  topics are subscribed), NOT `getConnection()==="disconnected"` on an idle/no-subscriber view (today's lie).
  Or fold it into the global banner.
- **Per-symbol freshness comes from `useInstrument`, not the container.** The container only sees declared
  static `dataSources`; per-symbol subs live inside `useInstrument` (#29) and are invisible to it. So
  `useInstrument` must return **tick age / freshness** (it currently discards `PriceDTO.ts_event`) → a
  per-symbol `live | stale` the row renders uniformly.
- **Uniform tile state at the container seam** (not just `TileFrame` — it misses `chrome:false` tiles like
  watchlist/portfolio/detail, which hand-roll "unavailable"/"loading" today). A `<TileStateShell>` the
  container wraps every tile in (chrome or not), rendering `loading | empty | stale | error` + a status pill
  (LIVE|STALE|DISCONNECTED|EMPTY|ERROR) + last-update time.
- **Add `EMPTY` to the status model** with a central empty-predicate per source, so "no positions" / "no
  symbols" / "no live data" stop being hand-rolled per tile.
- Freshness is **channel-specific + market-hours aware**: `ts_event` works for live trades; bars can be
  intentionally old by granularity; positions have no timestamp.

## (original) Design questions — answered above
1. **"API reachable"** — a lightweight app-level `/health` poll, or make the WS connection itself the
   liveness signal (proactively open + heartbeat-ping at app mount so "connected" = backend reachable,
   independent of tile subscriptions)? The lazy-WS "lie" argues for a proactive heartbeat.
2. **Staleness** — judge on the client (age since last frame > threshold, market-hours aware) or trust a
   backend feed-health flag (expose the Alpaca WS `connected` + last-tick age via `/health` or a WS
   `status` frame)? Client-side age is simplest and vendor-agnostic; backend flag is truer.
3. **Uniform surfacing** — one framework `useConnectionHealth() → {api, ws, fresh}` + a single global banner
   (degraded/disconnected/stale), plus the framework rendering per-tile `loading | empty | stale | error`
   consistently (SourceStatus already carries these; tiles shouldn't hand-roll). What's the seam — the
   container injects a uniform status chrome, or a shared `<TileState>` wrapper?
4. **Combining the three** into what the trader sees: precedence (api down > ws down > stale > live), copy,
   and where it lives (header badge + per-tile overlay).

## Acceptance
- [ ] API down → clear global "cockpit API unreachable" state (not silent empty tiles).
- [ ] WS dropped → visible, and it does NOT false-alarm on a view that simply has no WS subscriber.
- [ ] Feed stale (WS up, no ticks during market hours) → visibly flagged as stale, not shown as live.
- [ ] Per-tile `loading | empty | stale | error` rendered uniformly by the framework, not per tile.
- [ ] tsc/eslint clean.

## Out of scope
- Backend health-check depth (a `/health` that probes redis/engine could be a follow-on). Actual reconnect
  logic (already done in #28). Alerting/notifications.
