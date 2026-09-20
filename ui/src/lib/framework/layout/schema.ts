/**
 * Layout = serializable data. A named view (Trading / Risk / Research) is a list of placed tile
 * instances with grid geometry. Persisted to the backend (`GET/PUT /layouts`, #7 phase 5) and
 * validated at every boundary by these Zod schemas. Bump LAYOUT_SCHEMA_VERSION + add a migration
 * (see ./migrate) whenever the shape changes.
 */
import { z } from "zod";

export const LAYOUT_SCHEMA_VERSION = 1;

export const placedTileSchema = z.object({
  /** Unique per placed tile (NOT the type) — a chart×AAPL and chart×MSFT have distinct ids. */
  instanceId: z.string(),
  type: z.string(),
  /** Per-instance config; validated against the tile's own configSchema by the container. */
  config: z.unknown(),
  x: z.number(),
  y: z.number(),
  w: z.number(),
  h: z.number(),
});

export const layoutSchema = z.object({
  id: z.string(),
  name: z.string(),
  schemaVersion: z.number(),
  tiles: z.array(placedTileSchema),
});

export type PlacedTile = z.infer<typeof placedTileSchema>;
export type Layout = z.infer<typeof layoutSchema>;
