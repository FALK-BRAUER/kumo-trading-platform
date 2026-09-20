# Plan — Issue #25: Symbol search + symbol-search tile

Vertical slice: backend search endpoint → wire → new tile → `focusedSymbol` store. Design defaults from
Perplexity; hardened against a Codex plan review (both 2026-07-05).

## Architecture decision — search fits the Nautilus infra (via `Equity.info`)

Worked through A (raw Alpaca-dict scan) vs B (Nautilus `InstrumentProvider`). B initially looked broken
because Nautilus `Equity` has no company-name field — and search's core field IS the name. **Resolved:**
Nautilus `Instrument`/`Equity` carries an arbitrary `info: dict` (verified: `Equity(..., info={'name': …})`
accepted). So the name rides in-domain. Decision = **B**:

- The universe loads as Nautilus `Equity` domain objects (canonical `TICKER.MIC` ids, same as
  watchlist/positions/bars), **carrying the company name in `info`** — no parallel dict representation.
- Vendor seam stays at `parse_equity()` (the one place Alpaca→Nautilus mapping lives).
- **Not** provider-agnostic machinery: Alpaca is the decided provider (#23), so use
  `AlpacaInstrumentProvider` directly — no databento/ibkr instrument-search branches (YAGNI).
- Extending the *shared* `parse_equity()` means the engine's local instruments also carry names — a modest
  bonus (NOT auto-delivered to the UI: the RedisConsumer stream carries bars/positions, not instrument defs).

**Catalog ≠ subscription.** Search needs only the instrument *catalog* (one `/v2/assets` REST GET, ~13k
rows, cached hourly) — `load_all_async()` subscribes to nothing. Live market data stays **on-demand per
focused symbol** (one `bars` WS sub, same as existing tiles). No all-symbol data subscription anywhere.

## Backend (FastAPI, api process)

The api process runs **no Nautilus node** in `live` mode (only `RedisConsumer`, #20 split), so it loads its
**own** instrument catalog via `AlpacaInstrumentProvider` — independent of the engine cache (and of the
engine being up). A second REST `ClientSession` here is sound: no shared Nautilus cache, not a data websocket.

### Change: `backend/api/providers/alpaca/providers.py`
- Extend `parse_equity()` to set `info={"name": <asset name>}` (additive — engine ignores it). `name`
  missing/blank → fall back to `symbol` so the field is never null. Keep the `tradable` filter +
  `EXCHANGE_TO_MIC` mapping as-is.

### New: `backend/api/instrument_search.py`
- `InstrumentSearchIndex` — TTL-refreshed (default 3600s), holds one `AlpacaHttpClient` (connected).
  - On (re)load: build a **fresh** `AlpacaInstrumentProvider(client, config=None)` (Nautilus
    `InstrumentProviderConfig` slot — do NOT pass the Alpaca table), `await load_all_async()`, then derive a
    flat, pre-lowercased index from **`provider.get_all()`** (NOT `.list()` — that method doesn't exist):
    `{instrument_id, symbol, symbol_lc, name, name_lc, venue}` (name from `info["name"]`). **Fresh provider
    each reload** — the provider only `add()`s, never clears, so reusing it retains delisted/moved assets.
    Build the new index, then **swap it in atomically** (old index serves reads until the new one is ready).
  - `search(q, limit)`: trim `q`; whitespace-only/empty → `[]`. Lowercase once, single linear scan over
    ~13k rows, score, sort, slice top `limit`. Flat scan (no trie) — <50ms warm (Perplexity).
  - **Ranking tiers** (lower = better): 1 exact symbol · 2 symbol prefix · 3 symbol substring · 4 exact
    name · 5 name prefix · 6 name substring. Tie-break: shorter symbol/name → alphabetical by symbol →
    `instrument_id` (final deterministic tie-break for dup symbols/names).
  - **Refresh lifecycle (Codex-hardened):** non-blocking startup warm (background task in `lifespan`; don't
    block/fail startup if Alpaca slow/down; a search before warm awaits the in-flight load). `asyncio.Lock`
    reload that **double-checks freshness inside the lock**. No retry storm: on load fail/429 keep stale +
    short backoff; first-load fail with no data → `503`.

### Wiring: `backend/api/app.py`
- In `lifespan`: build the search index only when `feed_config.data_provider == "alpaca"`. Mirror
  `data_client.build_data`: read **env var names** from `feed_config.provider_config` (`key_env`/`secret_env`),
  resolve the keys via `os.environ`; **base URLs from `AlpacaDataClientConfig` defaults** unless the table
  overrides them (feed.toml only carries `key_env`/`secret_env`/`feed`). Create **one** `AlpacaHttpClient`,
  `connect()` it, hand that same instance to the index; on shutdown cancel/await the warm task then `close()`.
- **Missing keys / build failure must NOT fail lifespan** — wrap in try/except → `app.state.search = None`
  (search endpoint then 503). Dev/synthetic runs with `provider = "alpaca"` but no keys must still boot.
- New endpoint — **always registered** (stable OpenAPI/codegen):
  ```
  GET /instruments/search?q=<str>&limit=<int, default 20, clamp 1..50>
  → InstrumentSearchResponse(results=[InstrumentMatch, ...])
  ```
  - `operation_id="searchInstruments"`, `tags=["instruments"]`, typed `response_model` (feeds TS codegen).
  - `app.state.search is None` OR first-load failed with no data → `503` + clear message.
- CORS already `allow_methods=["GET"]` — no change.

### Models: `backend/api/models.py`
- `InstrumentMatch(instrument_id, symbol, name, venue)` + `InstrumentSearchResponse(results: list[...])`.

### Tests: `backend/api/test_instrument_search.py` + extend `test_app.py`
- Ranking order, empty/whitespace → [], limit clamp (1..50), TTL refresh reloads (monkeypatch clock + fake
  provider), lock double-check (one reload under concurrency), provider-unavailable/first-load-fail → 503,
  blank-name→symbol fallback, `info['name']` carried through. Fake provider/client returning fixture assets.

## Frontend (Next.js + TanStack Query)

### Store: `ui/src/lib/framework/store/index.ts`  *(the one framework-store change)*
- Add `focusedSymbol: string | null` + `setFocusedSymbol(id: string | null)`. Ephemeral client state, **no
  `layouts.ts` mutation** (config-over-editor). Future chart/detail tile reads it.

### Types codegen (Codex): run **`npm run gen:api`** after backend models land → re-export `InstrumentMatch`
/ `InstrumentSearchResponse` from `ui/src/lib/api/types.ts` (source of truth = generated `schema.ts`).

### REST client: `ui/src/lib/api/client.ts`
- `searchInstruments(q, limit, signal?)` — `URLSearchParams` encoding, `cache: "no-store"`, pass
  `AbortSignal`; surface non-ok (esp. 503) distinctly.

### Datasource declaration — DROPPED
- Both Codex passes flagged registering `instrument-search` as decorative (tile uses direct `useQuery`,
  `dataSources: []`). Skip it — misleading config. Document the framework gap (`useSource` binds static
  config, not the runtime typed query) in the tile README instead.

### New tile: `ui/src/tiles/symbol-search/` (definition.ts + SymbolSearchTile.tsx + README.md)
- `definition.ts`: `type: "symbol-search"`, minimal configSchema (`{ placeholder?: string }`),
  `dataSources: []`, default/min size, `chrome: false`.
- `SymbolSearchTile.tsx`: debounced input **250ms**, **min length 1** (one-letter tickers F/T/C/V valid),
  trim, whitespace→empty, guard IME composition. `useQuery` keyed on debounced value:
  `enabled: q.trim().length >= 1`, `queryFn: ({signal}) => searchInstruments(q, 20, signal)`,
  `placeholderData: keepPreviousData`, `staleTime: 5_000`, `retry: false`. Graceful **503 "search
  unavailable"** state. Ranked list: keyboard (↑/↓/Enter) + click → `setFocusedSymbol(instrument_id)`; show
  current focus.
- Register in `ui/src/config/tiles.ts`; place in a layout view in `ui/src/config/layouts.ts`.

### Frontend verification (no unit tests this slice)
- **No FE test runner exists** (`ui/package.json` has only `openapi-typescript`). Adding a harness is out of
  scope — verify the tile manually against the running paper stack. Backend keeps full unit tests.

## Out of scope
- Chart/detail tile consuming `focusedSymbol` (follow-on). Runtime watchlist editing. Order entry.
- Provider-agnostic instrument search (Alpaca is decided). FE unit-test tooling.

## Verification / AC mapping
- `GET /instruments/search?q=aapl` → `AAPL.XNAS` first (exact symbol); `?q=apple` → Apple first (name
  prefix). `?q=app` → Apple present (name substring) but not necessarily #1 (symbol hits like `APP` rank
  above) — AC reworded (Codex BLOCKER on original wording).
- `limit` respected & clamped 1..50; <~50ms warm.
- Tile: type → debounced results → keyboard + click select → `focusedSymbol` set, no `layouts.ts` mutation.
- Tile registered + placeable; README present. Backend unit tests pass. ruff + tsc/eslint clean.
