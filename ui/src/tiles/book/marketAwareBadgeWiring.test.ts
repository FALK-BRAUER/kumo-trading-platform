/** THE WIRING, not the helper (#873, same shape as #662). A tested badge helper that no tile renders is
 * the exact defect that shipped #644. No DOM here (vitest env node), so the pin is on the component
 * source: the badge is called with the per-lane value from the hook, in the returned JSX, on both tiles.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const book = readFileSync(join(__dirname, "BookTile.tsx"), "utf8");
const strategy = readFileSync(join(__dirname, "..", "strategy", "StrategyTile.tsx"), "utf8");

describe("the market-aware badge is rendered, not just defined", () => {
  it("BookTile threads the hook's value per lane into the cell and renders the badge there", () => {
    expect(book).toContain("const laneMarketAware = useLaneMarketAware();");
    expect(book).toContain("marketAware={laneMarketAware[r.label]}");
    const returned = book.slice(book.indexOf("const badge = marketAwareBadge(marketAware)"));
    expect(returned).toContain("title={badge.title}");
  });

  it("StrategyTile renders the badge for its own lane inside the returned JSX", () => {
    const returned = strategy.slice(strategy.indexOf("return ("));
    expect(returned).toContain("marketAwareBadge(laneMarketAware[laneId])");
  });
});
