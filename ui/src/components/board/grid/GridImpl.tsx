"use client";

/**
 * GridImpl — the actual react-grid-layout instance. Loaded ONLY via `GridShell` (dynamic, ssr:false)
 * because RGL/WidthProvider touch `window` at module scope. Never import this directly.
 *
 * Single-breakpoint cockpit → plain `GridLayout` wrapped in `WidthProvider` (not Responsive). Render-
 * only: `isDraggable/isResizable={false}` (no runtime editor — layouts are config). Geometry honors
 * each tile's exact x/y/w/h (`compactType={null}` + `preventCollision={false}`).
 */
// RGL + react-resizable global CSS is imported once in app/layout.tsx (App Router requires global
// stylesheets at the layout boundary), not here.
import GridLayout, { WidthProvider, type Layout as RglItem } from "react-grid-layout";

const ResponsiveGrid = WidthProvider(GridLayout);

/** Grid geometry constants — one breakpoint, fixed column count. */
export const GRID_COLS = 24;
export const GRID_ROW_HEIGHT = 20;
export const GRID_MARGIN: [number, number] = [12, 12];

export interface GridImplProps {
  layout: RglItem[];
  children: React.ReactNode;
}

export default function GridImpl({ layout, children }: GridImplProps) {
  return (
    <ResponsiveGrid
      className="cockpit-grid"
      layout={layout}
      cols={GRID_COLS}
      rowHeight={GRID_ROW_HEIGHT}
      margin={GRID_MARGIN}
      isDraggable={false}
      isResizable={false}
      measureBeforeMount={false}
      compactType={null}
      preventCollision={false}
    >
      {children}
    </ResponsiveGrid>
  );
}
