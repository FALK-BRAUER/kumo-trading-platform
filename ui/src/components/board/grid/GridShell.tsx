"use client";

/**
 * GridShell — client-only entry to the react-grid-layout instance. RGL + `WidthProvider` read `window`
 * at import time, so `GridImpl` is loaded via `dynamic(..., { ssr:false })`. The `loading` placeholder
 * is a fixed blank box so the server render and the first client render match (no hydration mismatch);
 * the grid mounts once the chunk arrives.
 */
import dynamic from "next/dynamic";
import type { GridImplProps } from "./GridImpl";

/**
 * Below this viewport width the Board stacks tiles instead of gridding them (#60). Lives HERE, not in
 * `GridImpl`, because Board must read it and importing GridImpl would pull RGL (which touches `window`
 * at module scope) into the Board bundle — the exact thing this file's dynamic import prevents.
 *
 * The column count is FIXED at 24, so a narrow viewport does not get fewer, wider columns — it gets 24
 * unusably thin ones. At 390px a half-width (w:12) tile is ~195px and its content wraps into a ribbon;
 * the absolute positioning also overflows the viewport horizontally. 900px keeps tablets on the grid
 * (24 cols ≈ 34px each).
 */
export const GRID_STACK_BELOW_PX = 900;

const GridImpl = dynamic(() => import("./GridImpl"), {
  ssr: false,
  loading: () => <div className="min-h-[60vh] w-full" aria-hidden />,
});

export function GridShell(props: GridImplProps) {
  return <GridImpl {...props} />;
}
