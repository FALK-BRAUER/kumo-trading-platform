# components/search

Global cockpit search UI (not tile-scoped).

- `GlobalSymbolSearch.tsx` — the header symbol search (#25). Debounced input + ranked dropdown over
  `GET /instruments/search`; selecting a match calls `openDetailForSymbol` (sets the focused instrument +
  opens the #27 detail surface). Mounted in the
  app header (`app/page.tsx`), inside `QueryProvider`. It is deliberately NOT a grid tile — it belongs in
  the top bar so it never consumes a view's tile space.

What doesn't go here: placeable board tiles (those live in `@/tiles`), data-fetch wiring (`@/lib/api`).
