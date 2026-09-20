"use client";

/**
 * Board — renders the active view's placed tiles on the grid by their x/y/w/h geometry. Read-only:
 * layouts come from the JSON/TS config (`@/config/layouts`), not a runtime editor. Switch views via the
 * tab strip; to change a layout, edit the config. Min sizes from each tile definition are carried into
 * the grid so the geometry stays sane.
 */
import "@/config/tiles"; // side-effect: populate the tile registry
import { PeriodSelector } from "@/components/ds/PeriodSelector";
import { UnitToggle } from "@/components/ds/UnitToggle";
import "@/config/datasources"; // side-effect: populate the data-source registry
import "@/config/checklists"; // side-effect: populate the checklist registry (#181)
import "@/config/kpis"; // side-effect: populate the KPI registry (#182 follow-up)
import { useEffect, useState } from "react";
import { TileContainer } from "@/lib/framework/container";
import { useCockpitStore } from "@/lib/framework/store";
import { getTile } from "@/lib/framework/registry";
import { tilesToRglLayout } from "@/lib/framework/layout/rgl";
import { GridShell, GRID_STACK_BELOW_PX } from "@/components/board/grid/GridShell";
import { DetailHost } from "@/components/board/DetailHost";
import { ViewTabs } from "@/components/board/ViewTabs";
import { RefreshBar } from "@/components/board/RefreshBar";
import { DEFAULT_LAYOUTS } from "@/config/layouts";

/**
 * True when the viewport is too narrow to divide into 24 usable grid columns (#60).
 *
 * Starts FALSE and flips on mount: `matchMedia` is client-only, and the grid itself is already
 * `ssr:false`, so the server never renders either branch's real content. Defaulting to false keeps the
 * desktop path free of a layout flash; a phone briefly shows the grid's blank placeholder instead.
 */
function useIsNarrow(): boolean {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${GRID_STACK_BELOW_PX - 1}px)`);
    const apply = () => setNarrow(mq.matches);
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);
  return narrow;
}

export function Board() {
  const setLayouts = useCockpitStore((s) => s.setLayouts);
  const layout = useCockpitStore((s) => s.activeLayout());
  const detailOpen = useCockpitStore((s) => s.detailOpen);
  const focus = useCockpitStore((s) => s.focus);
  const narrow = useIsNarrow();

  useEffect(() => {
    setLayouts(DEFAULT_LAYOUTS);
  }, [setLayouts]);

  if (!layout) return null;

  const stack = layout.tiles.length === 1 || narrow;

  // Detail surface (#27) replaces the grid region below the tabs — grid UNMOUNTED (not hidden: display:none
  // breaks RGL width measurement, and a mounted grid keeps every tile's WS sub alive). Tabs stay so the user
  // can leave; closing returns to the same config-driven grid.
  const showDetail = detailOpen && focus;

  // Carry each tile's minSize into the RGL layout so geometry stays within its usable minimum.
  const rglLayout = tilesToRglLayout(layout.tiles).map((item) => {
    const min = getTile(layout.tiles.find((t) => t.instanceId === item.i)!.type)?.minSize;
    return min ? { ...item, minW: min.w, minH: min.h } : item;
  });

  return (
    <div className="mx-auto max-w-screen-2xl px-4 py-6">
      <ViewTabs />
      {/* THE GLOBAL PERIOD CONTROL LIVES HERE, not in a tile (#233).
        *
        * It shipped mounted only inside EquityTile and gated on that tile's own `available` curves —
        * so on any view without the equity tile there was NO control at all, while BookTile went on
        * reading the shared `period` from the store. The state was global and the control was not:
        * REALIZED was stuck on whatever the store last held and nothing on screen could move it.
        *
        * No `available` prop, deliberately. That prop exists so the equity chart can grey out a curve
        * the broker did not return; here every period is answerable — an unswept one renders a dash,
        * which is a different and honest statement from a disabled tab. */}
      {!showDetail && (
        // THE TWO AXES OF ONE QUESTION, opposite each other on one row (#586): the unit on the left,
        // the window on the right. The toggle was first placed beside the BOOK heading — a DIFFERENT
        // row from the selector it is meant to pair with, leaving this row's left half empty (Operator,
        // 2026-09-06: "the placement is completely wrong"). Here it is also global, which matches the
        // store it reads and #392's cross-screen rule.
        <div className="mb-2 flex items-center justify-between">
          <UnitToggle />
          <PeriodSelector />
        </div>
      )}
      {showDetail ? (
        // DetailHost resolves (focus.kind, strategy) through the DetailRegistry and remounts on focus change.
        <DetailHost />
      ) : stack ? (
        // STACKED: skip the fixed-height RGL cell — it CLIPS tall content (an expanded watchlist card is
        // ~1100px vs a ~500px cell) and, on a phone, absolutely-positions tiles at pixel widths computed
        // from 24 columns, which overflows the viewport. Render at natural height and let the PAGE scroll.
        //
        // Applies when there is only ONE tile (nothing to lay out) OR the viewport is too narrow to divide
        // into 24 usable columns. Column count is fixed, so on a 390px phone a w:12 tile is ~195px and its
        // content wraps into a ribbon. This is #60: the trigger is available WIDTH, not tile count — Home
        // going from one tile to two is what surfaced it.
        <>
          <RefreshBar />
          {/* key by instanceId so switching views (portfolio→watchlist) REMOUNTS the tile — without it React
              reuses the instance across different tile component types and throws #300 (hooks mismatch). */}
          {/* `min-w-0` on BOTH the column and each child is load-bearing, not defensive. A flex item
              defaults to `min-width: auto`, so a tile containing a wide `overflow-x-auto` table (the
              watchlist: frozen first column, scrolls sideways INSIDE the tile) pushes the flex parent
              out to its content width instead of scrolling within it — and the whole page then
              overflows the viewport. The single-tile branch this replaced was a plain block div, where
              that could not happen; the regression arrived with the flex column. */}
          <div className="flex w-full min-w-0 flex-col gap-3">
            {[...layout.tiles]
              .sort((a, b) => a.y - b.y || a.x - b.x)
              .map((tile) => (
                <div key={tile.instanceId} className="w-full min-w-0">
                  <TileContainer tile={tile} />
                </div>
              ))}
          </div>
        </>
      ) : (
        <>
          <RefreshBar />
          <GridShell layout={rglLayout}>
            {layout.tiles.map((tile) => (
              <div key={tile.instanceId} className="h-full overflow-auto">
                <TileContainer tile={tile} />
              </div>
            ))}
          </GridShell>
        </>
      )}
    </div>
  );
}
