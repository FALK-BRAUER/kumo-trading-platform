/**
 * #808 items 1 and 3 — one word, one quantity; two derivations, both named.
 *
 * Measured 2026-09-09: the header's "standing" was Σ unrealized ($3,048.20) while each lane's
 * "standing" was realized-on-open-cycles + unrealized (MOMENTUM 20.39 + 530.24 = 550.63), so the
 * lanes summed to $3,110.69 — a 62.49 gap that was exactly Σ open-cycle partial realized. And the
 * lanes' "mark" figures (our marks vs the EOD base) summed to $3,099.39 under a header Δ UNREALIZED of
 * $2,250.90 (broker equity residual) with nothing saying they were different derivations.
 *
 * Rendered through the real BookTile with the live fixture, numbers parsed off the HTML.
 */
import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { createElement } from "react";
import type { SourceStatus, TileProps } from "@/lib/framework/types";
import type { BookConfig } from "./definition";
import { FRAME } from "@/tiles/managed-portfolio/liveFrame.fixture";
import { attribute, money } from "@/tiles/managed-portfolio/books";

const host = vi.hoisted(() => ({ period: "1M", ranges: new Map<string, number>() }));

vi.mock("@/lib/framework/store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useCockpitStore: (select: (s: unknown) => unknown) =>
    select({ period: host.period, unit: "$", openDetail: () => {}, openDetailForSymbol: () => {} }),
}));
vi.mock("@/lib/framework/instrument", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useTodayRanges: () => host.ranges,
}));

import { BookTile } from "./BookTile";

function render(base: unknown, external: unknown[] = []): string {
  const props: TileProps<BookConfig> = {
    instanceId: "book-1",
    config: {},
    data: { trades: FRAME, account: { account: null }, external_activity: { external }, equity_curve: null },
    status: { trades: "live" as SourceStatus },
    onConfigChange: () => {},
  };
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnMount: false } } });
  qc.setQueryData(["health"], {
    status: "ok", subsystems: [{ name: "engine", ok: true }], feed_last_tick_ts: Date.now() * 1_000_000,
    reconcile_drift: [], protection_divergence: [], inert: [], ownership_violations: [],
  });
  qc.setQueryData(["pnl-unrealized-base"], base);
  return renderToString(createElement(QueryClientProvider, { client: qc }, createElement(BookTile, props)))
    .replace(/<!--\s*-->/g, "")
    .replace(/&nbsp;/g, " ");
}

const num = (s: string) => Number(s.replace(/[$,]/g, ""));
/** Every lane sub-line `standing $X` (number AFTER the word). */
const laneStandings = (html: string) => [...html.matchAll(/standing (-?\$[\d,]+\.\d\d)/g)].map((m) => num(m[1]));
/** The header sub-line `$X standing` (number BEFORE the word). */
const headerStanding = (html: string) => {
  const m = html.match(/(-?\$[\d,]+\.\d\d) standing/);
  return m ? num(m[1]) : null;
};
const laneMarks = (html: string) => [...html.matchAll(/mark <span[^>]*>(-?\$[\d,]+\.\d\d)/g)].map((m) => num(m[1]));
const headerMarksSum = (html: string) => {
  const m = html.match(/lane marks Σ (-?\$[\d,]+\.\d\d)/);
  return m ? num(m[1]) : null;
};

const trades = (FRAME as { trades: Array<Record<string, unknown>> }).trades.filter((t) => t.is_engaged) as never[];

describe("the fixture can express the defect", () => {
  it("at least one lane carries realized P&L on an OPEN cycle — the 62.49 shape", () => {
    const open = attribute(trades, []).strategies.filter((s) => s.book.held > 0 && s.book.realized !== 0);
    expect(open.length).toBeGreaterThan(0);
    expect(trades.some((t) => money((t as { realized_pnl?: string }).realized_pnl) !== 0)).toBe(true);
  });
});

describe("standing means the same thing in the header and in every lane", () => {
  it("Σ lane standing == header standing, to the cent", () => {
    const html = render({ by_period: { "1M": null }, base_date: { "1M": null }, unreadable: [], error: null });
    const lanes = laneStandings(html);
    const header = headerStanding(html);
    expect(lanes.length).toBeGreaterThan(1); // vacuity guard: rows rendered
    expect(header).not.toBeNull();
    // Each lane is rounded to the cent before summing; the header rounds the exact sum once — at most
    // half a cent per rendered row of legitimate disagreement. The 62.49 was not that.
    expect(Math.abs(lanes.reduce((a, b) => a + b, 0) - (header as number))).toBeLessThan(0.005 * (lanes.length + 1));
  });
});

describe("the two Δ derivations are both on the panel, and the lanes' one adds up", () => {
  it("the header names Σ of the lanes' marks, and it equals the rendered marks", () => {
    const html = render({
      by_period: { "1M": { "MOMENTUM-002": -100 } }, base_date: { "1M": "2026-07-31" }, unreadable: [], error: null,
    });
    const marks = laneMarks(html);
    const shown = headerMarksSum(html);
    expect(marks.length).toBeGreaterThan(0);
    expect(shown).not.toBeNull();
    expect(Math.abs(marks.reduce((a, b) => a + b, 0) - (shown as number))).toBeLessThan(0.005 * (marks.length + 1)); // per-cell rounding
  });
  it("says nothing about Σ marks when no base exists — unknown, not zero", () => {
    const html = render({ by_period: { "1M": null }, base_date: { "1M": null }, unreadable: [], error: null });
    expect(headerMarksSum(html)).toBeNull();
  });
});

describe("a Σ over SOME lanes says so", () => {
  it("a lane whose base is explicitly null drops out and the coverage is shown", () => {
    const html = render({
      by_period: { "1M": { "MOMENTUM-002": -100, "MANUAL-001": null } }, base_date: { "1M": "2026-07-31" },
      unreadable: [], error: null,
    });
    const m = html.match(/lane marks Σ -?\$[\d,]+\.\d\d \((\d+)\/(\d+)\)/);
    expect(m).not.toBeNull();
    const [, known, all] = m as RegExpMatchArray;
    expect(Number(known)).toBeLessThan(Number(all));
    expect(laneMarks(html).length).toBe(Number(known));
  });
});

describe("phantoms are counted on the header, not summed (#808 item 4)", () => {
  const phantom = {
    instrument_id: "CRM.XNYS", source: "POSITION", side: "LONG", quantity: 10, origin: "RECONCILIATION",
    strategy_id: "EXTERNAL", realized_pnl: "0.00 USD", unrealized_pl: 21.05, market_value: 2484, venue_qty: 0,
  };
  it("renders '1 phantom' and leaves the standing figure as it was without the row", () => {
    const base = { by_period: { "1M": null }, base_date: { "1M": null }, unreadable: [], error: null };
    const with_ = render(base, [phantom]);
    const without = render(base, []);
    expect(with_).toMatch(/1 phantom/);
    expect(without).not.toMatch(/phantom/);
    expect(headerStanding(with_)).toBe(headerStanding(without));
  });
});
