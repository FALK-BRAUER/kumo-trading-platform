import { describe, expect, it } from "vitest";
import { DEFAULT_LAYOUTS } from "@/config/layouts";

/**
 * The chart itself is SVG geometry inside a component; what is worth pinning in a node test is the
 * LAYOUT contract around it — that Home actually carries the tile, in the intended order.
 * (Geometry is verified in the browser; jsdom does no layout, which is how two overflow bugs shipped
 * earlier tonight.)
 */
describe("Home carries the equity curve (#243)", () => {
  const home = DEFAULT_LAYOUTS.find((l) => l.id === "home")!;

  it("places the equity tile on Home", () => {
    expect(home.tiles.map((t) => t.type)).toContain("equity");
  });

  it("orders it below Book and above Strategy", () => {
    const byY = [...home.tiles].sort((a, b) => a.y - b.y).map((t) => t.type);
    expect(byY).toEqual(["book", "equity", "strategy"]);
  });

  it("gives it full width — a curve squeezed into half the grid is unreadable", () => {
    const equity = home.tiles.find((t) => t.type === "equity")!;
    expect(equity.w).toBe(24);
  });
});
