/**
 * THE WIRING, not the helper (#662). Bite evidence: deleting `{windowStrip}` from the JSX left the
 * whole suite green — the helper was correct, tested, and unrendered, which is the exact shape that
 * shipped #644 (a tested frame builder bypassed at the publish site). No DOM environment exists in
 * this suite (vitest env "node"), so the pin is on the component source, docstring-stripped by the
 * same reasoning as the backend's _src() tests.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const tile = readFileSync(join(__dirname, "StrategyTile.tsx"), "utf8");
const def = readFileSync(join(__dirname, "definition.ts"), "utf8");

describe("the window strip is actually rendered (#662)", () => {
  it("the JSX includes the strip, not just defines it", () => {
    const returned = tile.slice(tile.indexOf("return ("));
    expect(returned).toContain("{windowStrip}");
  });

  it("the strip's numbers come from the sweep helper", () => {
    expect(tile).toContain("laneWindowRealized(tradesFrame");
  });

  it("the tile subscribes to the trades frame that carries the sweep", () => {
    expect(def).toMatch(/dataSources:\s*\[[^\]]*"trades"/);
  });
});
