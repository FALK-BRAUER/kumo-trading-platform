<!--
Provenance: drafted by ledger-tool HQ (2026-06-26), the operator-endorsed, relayed via the predecessor-repo lead
for issue #7 (tile framework). Copied verbatim into the repo as the steering reference. The "static
mockup gate" in §3 was later dropped — the operator is live-driving the UI, which IS the look-approval — but
the layout, tile catalog, and real-book sample data remain the target.
-->

# kumo-trading-platform UI — Flexible Tile Framework Spec

*Author: ledger-tool HQ · 2026-06-26 · For: cockpit peer*
*Status: SPEC for steering. (Static-mockup gate dropped — the operator live-drives the look; framework + tiles per below.)*

## 0. Why this exists / what changed from predecessor-repo

predecessor-repo's UI is **NOT composable** — it's a tab-based monolith (`dashboard/page.tsx` → 10 hardcoded
tab components), with `MasterCard` as the only reusable piece (a 5-zone card-layout wrapper, not a tile system).
Data is REST + `setInterval` polling, no global store, no WS, no layout system.

kumo-trading-platform replaces that with a **true composable tile framework**: tiles are registered units, data
comes from named WS-backed sources, layouts are runtime-editable DB config. This is "the flexible framework."

**Reuse as-is (port these 3 first):** `MasterCard`+`Ring` (145 LoC, pure layout — copy verbatim),
`IchimokuChart` (663 LoC, self-contained, swap its `/api/ohlcv` fetch for a DataSource binding),
`OrderPanel` (208 LoC, swap `/api/orders/pending` POST for the cockpit order action).
All three live at `~/projects/predecessor-repo/ui/components/`.

---

## 1. Framework architecture (the "flexible" part)

Four pieces. Keep them dumb and render-only — no trading logic in the UI.

### 1a. Tile Registry
A tile is a registered, self-describing unit. One definition per tile *type*.

```ts
interface TileDefinition<Cfg = unknown> {
  type: string;                         // "holdings" | "ichimoku" | "order" | "account" | ...
  title: string;                        // default header label
  component: React.ComponentType<TileProps<Cfg>>;
  defaultSize: { w: number; h: number }; // in grid units
  minSize?: { w: number; h: number };
  configSchema?: ZodSchema<Cfg>;        // per-instance config (e.g. ticker, lane filter)
  dataSources: string[];               // names of DataSources this tile subscribes to
}

interface TileProps<Cfg> {
  instanceId: string;                  // unique per placed tile
  config: Cfg;                         // validated instance config
  data: Record<string, unknown>;       // resolved from bound dataSources (keyed by source name)
  onConfigChange: (next: Cfg) => void; // tile self-edits (e.g. change chart ticker)
}

const TileRegistry = new Map<string, TileDefinition>();
```

### 1b. Data Source Registry
Named sources. Tiles bind by name; the container handles subscribe/cache/teardown. **WS-first** (kills the
yfinance 429 polling problem); REST only for one-shot snapshots.

```ts
interface DataSource<T = unknown> {
  name: string;                        // "positions" | "quotes" | "orders" | "ohlcv" | "account" | "risk"
  kind: "ws" | "rest";
  topic?: string;                      // WS topic, e.g. "fills", "bars:{ticker}", "quotes"
  endpoint?: string;                   // REST path for snapshot / kind:"rest"
  params?: (cfg: unknown) => Record<string, string>; // derive params from tile config (e.g. ticker)
  cacheMs?: number;                    // dedupe identical snapshot pulls
}
```
One WS connection multiplexes all topics. Container subscribes on mount, unsubscribes on unmount, fans the
latest payload into each subscribing tile's `data[sourceName]`.

### 1c. Layout system (runtime-editable)
`react-grid-layout`. A **layout = a named view = a tab** (Trading / Risk / Research). Persisted as DB config
(backend `GET/PUT /layouts`), editable at runtime: add/remove/move/resize tiles, rename views.

```ts
interface PlacedTile { instanceId: string; type: string; config: unknown; x: number; y: number; w: number; h: number }
interface Layout { id: string; name: string; tiles: PlacedTile[] }
```

### 1d. TileContainer (generic glue)
Takes a `PlacedTile` → looks up `TileDefinition` → validates config → subscribes its `dataSources` →
renders `definition.component` with resolved `data`. Wraps in a chrome (title bar, drag handle, config gear,
remove). Tiles never fetch directly — the container owns all data wiring. This is the whole point: a new tile
= register a definition + name its data sources. No routing, no plumbing.

---

## 2. Tile catalog (v1)

| type | purpose | reuses | dataSources | config |
|---|---|---|---|---|
| `account` | net-liq, cash, leverage, day P&L, tape read banner | new | `account` | — |
| `holdings` | open positions as MasterCard rows: P&L%, stop, dist-to-stop, phase, lane badge | MasterCard | `positions`, `quotes` | lane filter |
| `watchlist` | graded candidates: BCT grade, action pill (BUY/WAIT/CHASE), buy-stop trigger | MasterCard | `watchlist`, `quotes` | source/lane filter |
| `ichimoku` | weekly/daily/intraday Ichimoku chart, per ticker | IchimokuChart | `ohlcv` (bound to cfg.ticker) | ticker, default TF |
| `order` | place/confirm order ticket | OrderPanel | `quotes` | prefill ticker |
| `orders` | pending orders + live fills feed | MasterCard | `orders` (WS fills) | lane filter |
| `risk` | portfolio heat, sector/correlation, per-lane sleeve allocation | new | `risk` | — |
| `bct` | latest ledger-provider/BCT post — flagged buys/sells | new | `bct` | — |
| `lanes` | MANUAL / ETF_AUTO / BCT_AUTO status + toggles | new | `lanes` | — |

**MasterCard is the row primitive for holdings/watchlist/orders** — same 5-zone layout (bar · ring · identity ·
pill · value) + expand panel; expand panel embeds `IchimokuChart` (embedded mode) + `OrderPanel`. This is the
proven cockpit interaction from predecessor-repo; keep it.

---

## 3. Default "Trading" layout (12-col grid)

```
┌───────────────────────────────────────────────────────────────────────┐
│ ACCOUNT  net-liq S$40.0k · cash $15.6k · lev 0.49 · day +$58 · TAPE: grind/defensive │  (full width, short)
├──────────────────────────────────┬────────────────────────────────────┤
│ HOLDINGS (MasterCard rows)        │ ICHIMOKU (selected ticker)          │
│ RBC +x% stop640 · SPG · ATI ·     │ weekly/daily/intraday candles +     │
│ RY · AIT · BNS · AUPH · CYRX      │ cloud, tenkan/kijun overlay         │
│ [lane badge per row]              │                                     │
├──────────────────────────────────┤                                     │
│ WATCHLIST (graded)                ├────────────────────────────────────┤
│ SMH WAIT(base) · CRDO WATCH ·     │ ORDER PANEL  BUY/SELL · MKT/LMT/STP │
│ CAT WAIT · MU EXTENDED            │ shares auto-calc from risk          │
├──────────────────────────────────┼────────────────────────────────────┤
│ RISK  heat · sector · lane sleeves│ BCT FEED  ledger-provider latest buys/sells  │
└──────────────────────────────────┴────────────────────────────────────┘
```
Plus a row of **view tabs** at top: `Trading | Risk | Research | + ` (saved layouts), and on each tile the
chrome (title bar + drag handle + gear + ✕) so the editable-layout intent reads.

**Sample data (the operator's real book — use it so the UI reads true):**
- Holdings: RBC, SPG, ATI, RY, AIT, BNS, AUPH, CYRX — stops RBC640/SPG220/RY201/AIT327/BNS84/ATI190/AUPH17.11/CYRX14.85.
- Account: net-liq ~S$40.0k, cash US$15.6k, lev 0.49, day +$58, tape "grind / defensive".
- Watchlist: SMH (WAIT — basing >671.83), CRDO (WATCH — +18% ext), CAT (WAIT — retest ~1023), MU (EXTENDED +39%).
- For CURRENT position truth (live stops/qty/prices) broker ledger-tool (trading lane) rather than guessing.

---

## 4. Build order
1. Framework core: TileRegistry + DataSourceRegistry + TileContainer + RGL layout + DB layout persistence.
2. Port MasterCard/Ring + IchimokuChart + OrderPanel as the first 3 registered tiles (swap their fetches for DataSource bindings).
3. WS client (one multiplexed conn) + backend topics: account, positions, quotes, orders/fills, ohlcv.
4. Remaining tiles: account, watchlist, risk, bct, lanes.
5. View tabs (saved layouts) + runtime layout editing.

## 5. Constraints (non-negotiable)
- UI is **render-only** — never calls broker/DB directly, no trading logic, no server secrets. All via `api/`.
- WS-first, no polling.
- Strict TS, functional components + hooks, Tailwind + shadcn/ui, lucide, lightweight-charts (per global stack).
- Every dir gets a README.
