# Concept — uniform tile data resolution (close the per-instance/runtime gap)

## FRAMEWORK DIRECTIVE (the principle every option must satisfy)
Tiles are **uniform**. A tile declares *what data it needs*; the **framework resolves it** — transport
(REST/WS), per-symbol subscription, live price, status — and injects it. **Tiles never hand-wire their own
subscriptions.** Any two tiles showing the same kind of data (a symbol's bars + live price + P&L) behave
**identically by construction** — not by each tile importing the same hooks and hoping they match.

## The gap (grounded in code)
The framework's contract: `TileDefinition.dataSources: string[]` → `TileContainer` calls
`useSources(def.dataSources, config)` → injects `{data, status}` as props. `useSource`'s own docstring:
*"Tiles never call this directly; the TileContainer does."*

**Reality — every symbol-bearing tile violates it.** `WatchlistTile`, `PortfolioTile`, `SymbolDetailSurface`
all set `dataSources: []` and call `useSource` (and now `useLivePrice`) **inside themselves**, per row:
- `useSources` binds sources to the tile's **static `config`** via `def.params?.(cfg)`. It resolves ONE
  whole-tile source (e.g. `positions`, REST, config-bound).
- But a watchlist has **N rows for a runtime-determined symbol set**; a detail surface follows the
  **focused** symbol; positions stream **their** instruments. A static, config-bound, single-source
  declaration cannot express "N per-instance subscriptions for a runtime symbol list."
- So each tile drops to `useSource("bars",{symbol})` per row + `useLivePrice(symbol)` by hand.

**This bypass IS the divergence.** Whole-tile/static sources go through the container (uniform); per-symbol/
runtime sources are hand-rolled per tile (divergent). That's why Watch is live and Portfolio wasn't, and why
"make it live" turns into patching each tile with another hook. Same gap first flagged in #25 (search
couldn't use the datasource layer either) and #27.

## FINALIZED DESIGN (Perplexity + Codex, 2026-07-06) — Option B, hardened

**One blessed per-symbol primitive; raw transport hooks go framework-private.**

- `useInstrument(symbol) → { bars, price, fills, status }` — the SOLE way a tile touches per-symbol market
  data. `price` = mark (live tick ?? last bar close), `status` = loading/live/stale, `fills` filtered to the
  symbol. Composed inside the hook from the transport layer via **pure helpers** (`markPrice`, `status`) so
  the semantics live in ONE place.
- **P&L is NOT on `useInstrument`** — it's **position-scoped** (a symbol can have N positions, keyed
  `strategy_id:instrument_id`). Expose a pure `computePnl(position, markPrice)` helper; `PortfolioTile`
  applies it per position using the position's `useInstrument(symbol).price`.
- **Enforce the directive**: `useSource` / `useLivePrice` become **framework-private** — tiles may import
  ONLY blessed domain hooks (`useInstrument`) + get static sources as props. A lint boundary (or module
  privacy) stops tiles reaching raw transport. Without this, B is just the bypass renamed (Codex BLOCKER).
- **Two-path model, documented**: `TileDefinition.dataSources` stays for **static whole-tile** sources
  (`positions` REST — stays container-injected; do NOT move into per-row hooks → dup subs / P&L drift).
  Per-symbol/runtime data → `useInstrument`. Coherent as long as raw hooks aren't tile-importable.
- **Re-render safety**: `useInstrument`/`useLivePrice` must read the shared `prices` plane through a
  **selector** (per-symbol slice), not scan the whole array each render — `wsManager.emit` already rebuilds
  every topic ref per message, so a naive compose fans re-renders across all rows. Fix the selector as part
  of this, or the "uniform" hook is also a perf regression.
- **Migration**: `WatchRow`, `PositionRow`, `SymbolDetailSurface` → `useInstrument`; delete the per-tile
  `useLivePrice` + raw `useSource("bars"/"fills")`. Include **fills** (portfolio bypasses there too). Revisit
  the empty-portfolio `prices` sub (feed-indicator honesty is #26, not this). `hybrid` stays aspirational —
  don't build on it (`useSource` routes hybrid→WS-only today).

## Options considered (chose B)
- **A — runtime-param sources.** Extend the datasource registry + `useSource` so a source can bind to
  **runtime params** (a row's symbol, the focused symbol), not only static `config`. The container/framework
  owns a sanctioned per-instance resolution path. Biggest change; most general.
- **B — a framework-owned instrument primitive.** One hook/component, e.g. `useInstrument(symbol) →
  {bars, price, pnl, status}`, that EVERY symbol-bearing tile uses identically. Bars + live price + status
  resolved in ONE place → tiles can't diverge. Smaller; directly enforces "work the same way." Tiles still
  call a hook, but a SINGLE framework hook, not ad-hoc `useSource`+`useLivePrice` combos.
- **C — container-resolved dynamic lists.** A tile declares "per-instance sources for this symbol list";
  the container renders/resolves a subscription per item and injects a keyed result map. Closest to the
  original "container injects, tile renders" contract; requires dynamic-hook handling (stable keys).

## Questions (Perplexity discussion + Codex review)
1. Which option best honors the directive without a rules-of-hooks minefield (dynamic per-symbol
   subscriptions in React)? Is B (one framework hook) the pragmatic enforcement, or does the container-
   injection contract (C) demand the general runtime-param source (A)?
2. What's the right seam for "live price + P&L" so it's defined ONCE and every tile inherits it (mark price,
   fallback to bar close, staleness) — a derived source, a selector, a hook?
3. Migration: watchlist / portfolio / detail all move to the uniform path; the per-tile `useLivePrice`
   patches get deleted. Any ordering/back-compat traps?
4. Does honoring the container-injection contract (tiles get data as props, never call hooks) survive
   per-symbol dynamism, or is a blessed framework hook the honest primitive here?

## Out of scope
- New data kinds (order book, options). The transport layer (ws-manager, api topics) — unchanged; this is
  the tile↔data resolution seam only.
