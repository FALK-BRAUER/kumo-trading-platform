/**
 * Pure converter from our serializable `PlacedTile[]` to react-grid-layout's `Layout` (items of
 * `{ i, x, y, w, h }`) for read-only geometry placement. The grid key, the RGL item `i`, and
 * `PlacedTile.instanceId` are the SAME string — match on it, never on array index.
 */
import type { Layout as RglItem } from "react-grid-layout";
import type { PlacedTile } from "./schema";

export function tilesToRglLayout(tiles: PlacedTile[]): RglItem[] {
  return tiles.map((t) => ({ i: t.instanceId, x: t.x, y: t.y, w: t.w, h: t.h }));
}
