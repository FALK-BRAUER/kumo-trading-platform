/**
 * Tile Registry — `type → TileDefinition`. Generic core: holds definitions, knows no concrete tile.
 * Config (`@/config/tiles`) populates it; the TileContainer reads it. Add a tile = register a
 * definition there + place its instance in a layout. No core changes.
 */
import type { TileDefinition } from "./types";

// Mixed Cfg types share one map — store as `unknown` def, recover the type at the typed register edge.
const registry = new Map<string, TileDefinition<unknown>>();

export function registerTile<Cfg>(def: TileDefinition<Cfg>): void {
  registry.set(def.type, def as unknown as TileDefinition<unknown>);
}

export function getTile(type: string): TileDefinition<unknown> | undefined {
  return registry.get(type);
}

export function allTiles(): TileDefinition<unknown>[] {
  return [...registry.values()];
}
