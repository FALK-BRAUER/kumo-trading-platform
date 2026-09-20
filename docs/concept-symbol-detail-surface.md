# Concept — Symbol detail surface (consume `focusedSymbol`)

## Problem
Search (#25) sets `focusedSymbol` in the store, but **nothing renders it** — dead end. Selecting a symbol
must open a **detail surface in the content area below the tab strip** showing that symbol (chart + quote +
levels), dismissible back to the current view. the operator's framing: a "**modal tile type** that takes the space
below the tab; when I search something it shows there."

## Where it lives (board anatomy)
`Board` = `<ViewTabs/>` (tabs) → `<RefreshBar/>` → `<GridShell>` (the tile grid). The detail surface
occupies the **GridShell region** (below tabs), as an overlay/replacement — the tabs stay visible so the
user can leave it.

## RESOLVED DESIGN (Perplexity scoping + Codex review, 2026-07-06)

- **Path A** — bespoke `SymbolDetailSurface` owned by `Board`. **Not** a modal-tile framework primitive
  (B only when ≥2 confirmed full-surface consumers share the same lifecycle — YAGNI now). `Board` is the
  correct seam; `TileContainer` expects a placed grid tile + static config → using it would fake a tile or
  start the primitive we're avoiding.
- **Presentation: replace the grid content region.** Refinement over "hide, don't unmount": **unmount the
  grid** while detail is open (`detailOpen ? <Surface/> : <GridShell…/>`). Reasons (Codex): `display:none`
  breaks RGL's `WidthProvider` (records width 0); keeping the grid mounted keeps every watchlist row's
  `bars` WS sub live. Layouts are config (no runtime editor), so the grid rebuilds identically on return —
  only tile-local UI state (e.g. an expanded row) resets, acceptable for glance-then-return.
- **Store:** add `detailOpen: boolean` + `focusedInstrument: {instrument_id, symbol, name, venue} | null`
  (hold the whole selected match, not just the id — the surface header needs `name`, which selection
  currently discards). Actions: `openDetailForSymbol(match)` (sets instrument + `detailOpen=true`),
  `closeDetail()` (only sets `detailOpen=false`; keeps `focusedInstrument`). Keep focus across tab switches.
- **Search wiring:** selection calls `openDetailForSymbol(match)`, NOT `setFocusedSymbol(id)` — else
  re-selecting the same symbol after close won't reopen (value unchanged).
- **Surface unmounts on close:** it returns `null` when `!detailOpen`. Because focus persists, a
  still-mounted surface would keep its `bars` sub alive after close — so gate render on `detailOpen`.
- **Data:** surface owns the `bars` sub via `useSource("bars",{symbol})` keyed on the focused symbol.
  Teardown is clean (verified: `useSource` releases listener+topic on cleanup; `wsManager` unsubscribes at
  refcount 0) — so unmount-on-close + symbol-change both tear down correctly. No per-searched-symbol leak.
- **Mobile:** full-screen (not a bottom sheet — chart+quote is too dense). Header/search live OUTSIDE
  `Board` (in `page.tsx`), so a Board-slot surface sits below the header; define z-index so a full-viewport
  mobile surface doesn't fight the search dropdown (search closes on select anyway).
- **States:** reuse the watchlist `loading` / `no feed` status pattern — don't hand empty bars to a blank
  `ChartTile`. Symbol not in the feed universe → clear "no live data" state, not a dead chart.
- **Esc:** scope it — the search dropdown already consumes Esc; a board-level Esc-closes-detail handler must
  not fire while the user is typing in the search input.
- **ChartTile caveat:** it has a fixed 320px height + its own fullscreen overlay. v1 acceptable; a true
  full-region chart needs a height tweak.

## Original scoping question — A vs B (answered: A)

**A. Bespoke overlay component.** A `SymbolDetailSurface` rendered in `Board`, reads `focusedSymbol`,
renders chart/quote/levels, X/Esc to close (clears focus). No framework change. Fast, specific, one purpose.

**B. Generalized "modal/surface tile" framework primitive.** Extend the tile framework with a
render-as-full-area-modal mode: a store-driven `activeModal` slot renders a registered tile over the board
content, independent of grid geometry. `symbol-detail` becomes the first modal tile; later ones (order
ticket, settings, alerts) reuse the same mechanism. More surface area, reusable, but a real framework change
(registry + container + store + Board overlay slot).

the operator's words ("modal tile **type**") lean B. But B is only worth it if a *second* modal consumer is likely
soon. Scope this explicitly — don't build a framework primitive for one use (the #25 YAGNI lesson).

## Content of the surface (either path)
Reuse existing pieces — this is not new data work:
- `ChartTile` (already used in `WatchlistTile`'s expanded panel: `<ChartTile instrumentId bars />`).
- `bars` WS source for `focusedSymbol` (`useSource("bars",{symbol})`), same on-demand per-symbol path.
- Ichimoku levels/recommendation (`@/lib/ichimoku`) + last price, like `WatchRow`.
- Header: symbol + company name (name now available via `Equity.info` / search result) + close button.

## Open questions (for Perplexity scoping + Codex review)
1. **Overlay-over-grid vs replace-grid vs dedicated auto-view.** Modal overlay covering the grid (grid
   intact underneath), OR swap the content region entirely, OR a synthetic "Detail" tab that auto-activates?
   Which fits a keyboard-driven trading cockpit + mobile?
2. **A vs B** — bespoke overlay now, or the general modal-tile primitive? What triggers choosing B?
3. **Lifecycle of `focusedSymbol`** — does closing clear it? Does switching tabs keep it? Does re-searching
   the same symbol re-open? Should focus persist so other (grid) tiles can also react later?
4. **Mobile** — full-screen sheet vs the same overlay? (Header search + narrow width; the screenshot that
   triggered this was mobile.)
5. **Data lifecycle** — does the modal tile subscribe `bars` via the container (like grid tiles) or
   self-source? Unsubscribe on close so we don't leak a WS sub per searched symbol.
6. **Dismiss affordances** — X, Esc, click-outside, back-gesture (mobile). Which are in scope.

## Out of scope (initial)
- Order entry from the detail surface. Multi-symbol compare. Persisted "recently viewed".
