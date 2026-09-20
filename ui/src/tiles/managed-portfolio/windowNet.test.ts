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

import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { laneFigure } from "@/components/ds/unit";
import { windowNet } from "@/lib/framework/windowBase";

import { attribute, cellHeadline } from "./books";
import { FRAME } from "./liveFrame.fixture";

// ONE period, ONE unit, for both tiles (the store is the seam both read).
vi.mock("@/lib/framework/store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useCockpitStore: (select: (s: unknown) => unknown) =>
    select({ period: "1W", unit: "$", openDetail: () => {}, openDetailForSymbol: () => {} }),
}));
vi.mock("@/lib/framework/instrument", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useTodayRanges: () => new Map<string, number>(),
  useBars: () => [],
  useInstrument: () => ({ bars: [], price: null, quote: null, vwap: null, todayRange: null, fundamentals: null, fills: [], status: "live" }),
}));

const { BookTile } = await import("@/tiles/book/BookTile");
const { ManagedPortfolioTile } = await import("./ManagedPortfolioTile");

function _client(base: unknown): QueryClient {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnMount: false } } });
  qc.setQueryData(["health"], { status: "ok", subsystems: [{ name: "engine", ok: true }],
    feed_last_tick_ts: Date.now() * 1_000_000, reconcile_drift: [], protection_divergence: [], inert: [], ownership_violations: [] });
  qc.setQueryData(["pnl-unrealized-base"], base);
  return qc;
}
/** RAW html — the descriptor lives in a `title=` attribute and tag-stripping would erase it. */
const _raw = (html: string) => html.replace(/<!--\s*-->/g, "");

function renderBook(base: unknown): string {
  return _raw(renderToString(createElement(QueryClientProvider, { client: _client(base) },
    createElement(BookTile as never, { instanceId: "b", config: {}, onConfigChange: () => {},
      data: { trades: FRAME, account: { account: null }, external_activity: { external: [] }, equity_curve: null },
      status: { trades: "live" } } as never))));
}
function renderPortfolio(base: unknown): string {
  return _raw(renderToString(createElement(QueryClientProvider, { client: _client(base) },
    createElement(ManagedPortfolioTile as never, { instanceId: "p", config: {}, onConfigChange: () => {},
      data: { trades: FRAME, account: { account: null }, managers: { managers: [] }, external_activity: { external: [] } },
      status: { trades: "live", account: "live", managers: "live", external_activity: "live" } } as never))));
}
/** The MOMENTUM-002 lane CELL, by its `data-lane` handle (both tiles carry it on the cell root):
 *  the cell's own html up to the next cell root. Lane ORDER and neighbours do not matter (review,
 *  h2ho0jjf). Empty when the cell is not rendered — which fails the assertions, never passes them. */
function momentumCell(html: string): string {
  const m = html.match(/data-lane="MOMENTUM-002"[^>]*>([^]*?)(?:<div[^>]*data-lane=|$)/);
  return m ? m[1] : "";
}
/** Visible text of a cell (tags stripped) — the headline figure is text; the descriptor is a title. */
const _text = (html: string) => html.replace(/<[^>]+>/g, "|").replace(/&amp;/g, "&").replace(/\|+/g, "|");

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
  /**
   * #1133 — this used to pin the SPELLING of the call (`cellHeadline(period, day, …, net)` as a
   * source regex) and went red on 2026-09-15 when #1094 renamed BookTile's locals to `rowDay` /
   * `rowNet` without changing the rule. A spelling pin detects renames, not rules. What must hold
   * is BEHAVIOUR: for one lane, one frame and one served base, BOTH tiles render the headline that
   * `cellHeadline(period, day, periodRealized, unrealizedDelta, windowNet(mv_now, net, lane))`
   * yields — the SAME number, formatted by the same `laneFigure`. Driven through the rendered
   * tiles with the base seeded into react-query (the `bookWindowDelta.test.ts` harness) so a tile
   * that stopped threading the base, or computed its own net, is what goes red — not a variable name.
   */
  const NET_1W = { mv_base: 1_500, invested: 200, partial: null };   // MOMENTUM-002, window 1W

  it("FIXTURE: the frame's lane has a mark, and the served net terms yield a number no other figure in the frame equals", () => {
    const frame = FRAME as { trades: Array<{ strategy_id: string; is_capital_deployed: boolean; market_value: number }>;
                             realized_periods: Record<string, { by_strategy?: Record<string, number> }> };
    const mvNow = frame.trades
      .filter((t) => t.strategy_id === "MOMENTUM-002" && t.is_capital_deployed)
      .reduce((a, t) => a + t.market_value, 0);
    expect(mvNow).toBeCloseTo(1850.85, 2);
    const expected = windowNet(mvNow, { "MOMENTUM-002": NET_1W }, "MOMENTUM-002").value;
    expect(expected).toBeCloseTo(150.85, 2);                          // 1850.85 − 1500 − 200
    // No swept realized figure, nor the lane's raw market value, nor mv_now − mv_base (a net that
    // forgot the flows), nor another lane's absent-net fallback (mv_now itself) coincides with it —
    // the assertion below cannot pass on a cell that rendered one of those instead.
    const decoys = [...Object.values(frame.realized_periods["1W"].by_strategy ?? {}), mvNow, mvNow - NET_1W.mv_base];
    expect(decoys.map((v) => Math.round(v * 100))).not.toContain(Math.round(expected! * 100));
  });

  it("FIXTURE: in this harness the lane has NO sleeve, so `laneFigure(v, \"$\", null)` is the exact text both cells render", () => {
    // The cells format through `laneFigure(v, unit, sleeve)`; with unit "$" (the store mock) the
    // sleeve is unused, but a percent unit would divide by it — so the harness states that no
    // `/strategies` row was seeded (react-query has no data → `useSleeves()` yields nothing).
    const qc = _client(null);
    expect(qc.getQueryData(["strategies", "rows"])).toBeUndefined();
  });

  it("both tiles render MOMENTUM's 1W headline as the net of flows, the same number, from one rule", () => {
    const mvNow = 1850.85;
    const expected = cellHeadline("1W", undefined, -34.26, null,
      windowNet(mvNow, { "MOMENTUM-002": NET_1W }, "MOMENTUM-002"));
    expect(expected.kind).toBe("net");
    const figure = laneFigure(expected.value, "$", null);
    const base = { by_period: { "1W": null }, base_date: { "1W": "2026-09-11" }, market_value: { "1W": null },
                   net: { "1W": { "MOMENTUM-002": NET_1W } }, unreadable: [], error: null };
    const book = momentumCell(renderBook(base));
    const portfolio = momentumCell(renderPortfolio(base));
    expect(_text(book), "BookTile").toContain(figure);
    expect(_text(portfolio), "ManagedPortfolioTile").toContain(figure);
    // The DESCRIPTOR names the rule the figure came from (a `title=` attribute, hence raw html).
    expect(book).toContain("ΔMV − flows over");
    expect(portfolio).toContain("ΔMV − flows over");
    // And WITHOUT the served net, neither tile shows that number and both name the FALLBACK rule —
    // a positive counterpart, so this half cannot pass merely because the formatter differed.
    const noNet = { ...base, net: { "1W": null } };
    const bookNoNet = momentumCell(renderBook(noNet));
    const portfolioNoNet = momentumCell(renderPortfolio(noNet));
    expect(_text(bookNoNet)).not.toContain(figure);
    expect(_text(portfolioNoNet)).not.toContain(figure);
    expect(bookNoNet).toContain("REALIZED over");
    expect(portfolioNoNet).toContain("REALIZED over");
    expect(bookNoNet).not.toContain("ΔMV − flows");
    expect(portfolioNoNet).not.toContain("ΔMV − flows");
  });
});
