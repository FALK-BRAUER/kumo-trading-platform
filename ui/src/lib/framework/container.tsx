"use client";

/**
 * TileContainer — the generic glue. Takes a PlacedTile → looks up its TileDefinition → validates the
 * instance config → renders the component, wrapped in chrome (unless the tile renders its own).
 *
 * Tiles never fetch: the container will own data subscription (DataSource registry, #7 phase 2/3) and
 * pass resolved `data`/`status` down. For now those are empty — the migrated tiles still self-source
 * until that phase lands. Unknown type / bad config degrade to a visible fallback, never a crash.
 */
import { TileFrame } from "@/components/board/TileFrame";
import { getTile } from "./registry";
import { useCockpitStore } from "./store";
import { useSources } from "./datasource/useSource";
import type { PlacedTile } from "./layout/schema";

function FallbackTile({ title, reason }: { title: string; reason: string }) {
  return (
    <div className="rounded-xl border border-dashed border-amber-900/60 bg-amber-950/20 px-4 py-6 text-center">
      <p className="font-mono text-xs font-bold text-amber-400">{title}</p>
      <p className="mt-1 font-mono text-[11px] text-amber-500/70">{reason}</p>
    </div>
  );
}

export function TileContainer({ tile }: { tile: PlacedTile }) {
  const updateTileConfig = useCockpitStore((s) => s.updateTileConfig);
  const def = getTile(tile.type);

  // Validate instance config against the tile's own schema; fall back to its defaults on mismatch
  // (a saved layout from an older schema must not crash the board).
  const parsed = def?.configSchema.safeParse(tile.config);
  const config = parsed?.success ? parsed.data : (def?.defaultConfig ?? {});

  // Resolve the tile's declared sources → data/status. Called unconditionally (empty list for an
  // unknown type) so hook order is stable; the list is fixed per tile type.
  const { data, status } = useSources(def?.dataSources ?? [], config);

  if (!def) {
    return <FallbackTile title={tile.type} reason={`Unknown tile type "${tile.type}"`} />;
  }

  const Component = def.component;
  const node = (
    <Component
      instanceId={tile.instanceId}
      config={config}
      data={data}
      status={status}
      onConfigChange={(next) => updateTileConfig(tile.instanceId, next)}
    />
  );

  if (def.chrome === false) return node;
  return (
    <TileFrame title={def.title} bleed={def.bleed}>
      {node}
    </TileFrame>
  );
}
