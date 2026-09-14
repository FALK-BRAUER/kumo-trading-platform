# components/board

The board shell — the chrome around the tile grid.

- `Board.tsx` — composition root: `ViewTabs` → (`RefreshBar` + `GridShell` grid) OR the `SymbolDetailSurface`
  when a symbol's detail is open (#27). Reads layouts from config (`@/config/layouts`), no runtime editor.
- `ViewTabs.tsx` — the read-only view/tab strip.
- `RefreshBar.tsx` — live-refresh countdown heartbeat.
- `TileFrame.tsx` — the card chrome around a tile (title/border), skipped by `chrome: false` tiles.
- `SymbolDetailSurface.tsx` — full-region symbol detail (chart + quote + signal), shown in place of the grid.
- `grid/` — react-grid-layout integration (see its README).

What doesn't go here: tile implementations (`@/tiles`), framework/registry/store (`@/lib/framework`).
