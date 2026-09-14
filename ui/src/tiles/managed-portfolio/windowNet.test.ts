/**
 * The per-lane cell headline for a WINDOW is the lane's NET OF FLOWS (#699 option a).
 *
 * `net(W) = mv_now − mv_base − invested(W)`: the lane's change in market value over the window minus
 * the cash it put in. No basis rule — the FIFO-vs-average-cost counter-example that made
 * `realized(W) + Δunrealized(W)` wrong (and which `cellCarriedDelta.test.ts` still pins as NOT
 * summed) nets to exactly zero here. It is the identity DELTA NET uses at account level (equity
 * change net of flows), so the lane cells compose like the headline above them.
 *
 * `mv_base`, `invested` and the NAMED partial reason come from `GET /pnl/unrealized-base` (`net`);
 * `mv_now` from the live frame (`Book.marketValue`, signed). `windowNet` subtracts; `cellHeadline`
 * decides what the cell leads with — ONE rule for both tiles, as before.
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { windowNet } from "@/lib/framework/windowBase";
import { attribute, cellHeadline } from "./books";

const NO_DAY = undefined;

describe("the identity, through the served terms", () => {
  // Buy 50@10 + 50@20 (avg 15), base mark 20 → MV_base 2000. Sell 50@20 inside W, mark flat →
  // MV_now 1000, invested −1000. realized+Δunrealized said 250; the truth is 0.
  it("nets the counter-example to ZERO", () => {
    const net = windowNet(1000, { "MOMENTUM-002": { mv_base: 2000, invested: -1000, partial: null } }, "MOMENTUM-002");
    expect(net.value).toBe(0);
    const h = cellHeadline("1W", NO_DAY, 500 /* FIFO realized, carried in small print */, -250, net);
    expect(h.kind).toBe("net");
    expect(h.value).toBe(0);
  });

  it("holds with buys AND sells inside the window", () => {
    // base MV 10,000; bought 3,000 and sold 1,200 in W (invested +1,800); now worth 12,500 →
    // net = 12,500 − 10,000 − 1,800 = +700.
    const net = windowNet(12_500, { "TECHIVOL-005": { mv_base: 10_000, invested: 1_800, partial: null } }, "TECHIVOL-005");
    expect(net.value).toBeCloseTo(700, 6);
  });

  it("a lane BORN inside the window is partial, NAMED, with mv_base zero — its net is flows-vs-now", () => {
    const net = windowNet(2_850, { "QC345-003": { mv_base: 0, invested: 2_803.8, partial: "first observed 2026-09-11" } }, "QC345-003");
    expect(net.value).toBeCloseTo(46.2, 6);
    expect(net.partial).toBe("first observed 2026-09-11");
    const h = cellHeadline("1W", NO_DAY, 0, null, net);
    expect(h.kind).toBe("net");
    expect(h.kind === "net" && h.partial).toBe("first observed 2026-09-11");
  });

  it("a #1040 gap inside the window is partial and NAMED — the number stands, the interior is not bridged", () => {
    const net = windowNet(16_318.08, { "MOMENTUM-002": { mv_base: 26_353.68, invested: -7_837.58, partial: "not captured: 2026-09-09, 2026-09-10" } }, "MOMENTUM-002");
    expect(net.value).toBeCloseTo(-2_198.02, 2);
    expect(net.partial).toBe("not captured: 2026-09-09, 2026-09-10");
  });
});

describe("what unknown must NOT become", () => {
  it("no net map for the period → unknown, and the cell falls back to window realized as before", () => {
    const net = windowNet(1000, null, "MOMENTUM-002");
    expect(net.value).toBeNull();
    const h = cellHeadline("1M", NO_DAY, 3085.56, -257.41, net);
    expect(h.kind).toBe("window");
    expect(h.value).toBeCloseTo(3085.56, 2);
  });

  it("an unknown term (mv_base or invested null) → unknown, never zero", () => {
    expect(windowNet(1000, { A: { mv_base: null, invested: -5, partial: null } }, "A").value).toBeNull();
    expect(windowNet(1000, { A: { mv_base: 5, invested: null, partial: null } }, "A").value).toBeNull();
  });

  it("an unknown LIVE market value (an unmarked leg) → unknown", () => {
    expect(windowNet(null, { A: { mv_base: 5, invested: 1, partial: null } }, "A").value).toBeNull();
    expect(windowNet(NaN, { A: { mv_base: 5, invested: 1, partial: null } }, "A").value).toBeNull();
  });

  it("a lane ABSENT from a served map held nothing at the base and traded nothing after: net = mv_now, and it SAYS so", () => {
    const n = windowNet(120, { B: { mv_base: 1, invested: 1, partial: null } }, "A");
    expect(n.value).toBe(120);
    expect(n.partial).toMatch(/not in the served terms/);
    expect(windowNet(0, { B: { mv_base: 1, invested: 1, partial: null } }, "A").partial).toBeNull();
  });

  it("the Unclaimed row reads the EXTERNAL key, as windowDelta does", () => {
    expect(windowNet(10, { EXTERNAL: { mv_base: 4, invested: 1, partial: null } }, "Unclaimed").value).toBe(5);
  });
});

describe("what does not change", () => {
  it("1D keeps the day move even when a net is known", () => {
    const day = { value: -393.54, covered: 3, missing: 0 } as never;
    const h = cellHeadline("1D", day, 0, null, windowNet(1000, { A: { mv_base: 0, invested: 0, partial: null } }, "A"));
    expect(h.kind).toBe("day");
    expect(h.value).toBeCloseTo(-393.54, 2);
  });

  it("the FIFO realized and the carried mark delta still ride the net headline for the small print", () => {
    const h = cellHeadline("1W", NO_DAY, 297.92, 40, windowNet(1000, { A: { mv_base: 900, invested: 0, partial: null } }, "A"));
    expect(h.kind).toBe("net");
    expect(h.unrealizedDelta).toBe(40);
  });
});

describe("the live market value the cell subtracts from", () => {
  const t = (strategy_id: string, market_value: number | null, deployed = true) =>
    ({ strategy_id, market_value, is_capital_deployed: deployed, realized_pnl: "0", unrealized_pl: 0, quantity: 1 }) as never;

  it("is the SIGNED sum of the lane's deployed market values", () => {
    const a = attribute([t("A", 100), t("A", -30), t("B", 7)], []);
    expect(a.strategies.find((s) => s.strategyId === "A")?.book.marketValue).toBe(70);
    expect(a.strategies.find((s) => s.strategyId === "B")?.book.marketValue).toBe(7);
  });

  it("is UNKNOWN when any deployed position has no mark — never a smaller total", () => {
    const a = attribute([t("A", 100), t("A", null)], []);
    expect(a.strategies[0].book.marketValue).toBeNull();
  });

  it("ignores positions that are not capital-deployed", () => {
    const a = attribute([t("A", 100), t("A", 999, false)], []);
    expect(a.strategies[0].book.marketValue).toBe(100);
  });
});

describe("ONE rule, TWO tiles", () => {
  it("both tiles compute the net with `windowNet` and hand it to `cellHeadline`", () => {
    for (const f of ["src/tiles/book/BookTile.tsx", "src/tiles/managed-portfolio/ManagedPortfolioTile.tsx"]) {
      const src = readFileSync(f, "utf8");
      expect(src, f).toMatch(/windowNet\(/);
      expect(src, f).toMatch(/cellHeadline\(period, day, periodRealized, unrealizedDelta, net\)/);
      expect(src, f).toMatch(/ΔMV − flows/);
    }
  });
});
