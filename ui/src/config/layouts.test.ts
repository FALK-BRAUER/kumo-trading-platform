/**
 * Layout invariants. `layouts.ts` is pure data (no JSX), so it is testable directly — the tile
 * REGISTRY cannot be imported here because registering pulls components through a JSX transform.
 *
 * Exists because Home shipped as a single tile and stayed that way unnoticed: nothing asserted what
 * the landing view is supposed to contain, so "the homescreen is unchanged" was invisible to CI.
 */
import { describe, expect, it } from "vitest";
import { DEFAULT_LAYOUTS } from "./layouts";
import { LAYOUT_SCHEMA_VERSION } from "@/lib/framework/layout/schema";

const GRID_COLS = 24;

describe("DEFAULT_LAYOUTS", () => {
  it("lands on Home — a homescreen that is not the first thing you see is just another tab", () => {
    expect(DEFAULT_LAYOUTS[0].id).toBe("home");
  });

  it("puts the money above the machine on Home (Option A composition)", () => {
    const home = DEFAULT_LAYOUTS.find((l) => l.id === "home")!;
    const types = [...home.tiles].sort((a, b) => a.y - b.y).map((t) => t.type);
    expect(types[0]).toBe("book"); // P&L hero
    expect(types).toContain("strategy"); // then what the strategy did
  });

  it("gives every view a unique id and every tile a unique instanceId within it", () => {
    const ids = DEFAULT_LAYOUTS.map((l) => l.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const layout of DEFAULT_LAYOUTS) {
      const instances = layout.tiles.map((t) => t.instanceId);
      expect(new Set(instances).size, `duplicate instanceId in "${layout.id}"`).toBe(instances.length);
    }
  });

  it("keeps every tile inside the 24-column grid", () => {
    for (const layout of DEFAULT_LAYOUTS) {
      for (const t of layout.tiles) {
        expect(t.x, `${layout.id}/${t.instanceId} x`).toBeGreaterThanOrEqual(0);
        expect(t.w, `${layout.id}/${t.instanceId} w`).toBeGreaterThan(0);
        expect(t.x + t.w, `${layout.id}/${t.instanceId} overflows the grid`).toBeLessThanOrEqual(GRID_COLS);
        expect(t.h, `${layout.id}/${t.instanceId} h`).toBeGreaterThan(0);
      }
    }
  });

  it("never overlaps two tiles in the same view", () => {
    for (const layout of DEFAULT_LAYOUTS) {
      const ts = layout.tiles;
      for (let i = 0; i < ts.length; i++) {
        for (let j = i + 1; j < ts.length; j++) {
          const a = ts[i];
          const b = ts[j];
          const overlaps =
            a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
          expect(overlaps, `${layout.id}: ${a.instanceId} overlaps ${b.instanceId}`).toBe(false);
        }
      }
    }
  });

  it("stamps every view with the current schema version", () => {
    for (const layout of DEFAULT_LAYOUTS) {
      expect(layout.schemaVersion, layout.id).toBe(LAYOUT_SCHEMA_VERSION);
    }
  });
});
