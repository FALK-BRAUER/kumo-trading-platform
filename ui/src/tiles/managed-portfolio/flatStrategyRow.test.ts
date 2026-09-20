/**
 * THE DEFAULT PORTFOLIO TILE HIDES A FLAT STRATEGY'S REALIZED P&L (#523, second instance).
 *
 * #523 was fixed for `BookTile` and never for this tile. `ManagedPortfolioTile` renders the same
 * per-strategy breakdown from `visibleBooks(attribute(trades, external))` — ONE argument, so it gets
 * the INTERSECTION of live-cycle and swept strategies instead of the union. A strategy holding
 * nothing has no live trade cycle (a CLOSED cycle is emitted once and dropped from the projection),
 * so it has no book, no row, and its realized P&L renders nowhere.
 *
 * This tile is `registerTile(managedPortfolioDefinition)` and `layouts.ts:35` — the DEFAULT portfolio
 * view at w24 h24. So the panel most likely to be read is the one still carrying the defect.
 *
 * MEASURED ON A LIVE PAPER STACK, 2026-08-29. The fixture below is not invented: the four cycles
 * are copied VERBATIM out of `ui:state:trades` (one per strategy, all 25 fields intact) and
 * `realized_periods` is the engine's own sweep, unmodified.
 *
 *     live trade cycles   -> BCTROT-004, MOMENTUM-002, QC345-003, TECHIVOL-005   (no MANUAL-001)
 *     realized_periods 1M -> MANUAL-001 +1509.72   (2nd largest, behind MOMENTUM-002 +3085.56)
 *
 * MANUAL-001 is the operator's own discretionary lane, it is flat, and +1509.72 of realized money is
 * invisible on the default portfolio screen.
 *
 * DRIVEN THROUGH THE REAL COMPONENT, not through `visibleBooks`. `books.test.ts` already pins the
 * union thoroughly and every one of those tests passed while this tile was broken — because the
 * defect is in the CALL, not in the function. So this renders the actual tile and reads the actual
 * output: test the seam, not the unit. Only the two hooks that need a host (`useCockpitStore`,
 * `useTodayRanges`) are substituted; the component itself is the real one.
 *
 * MANUAL-001 can appear in exactly one place in this output. It has no cycle, so it cannot reach the
 * position table or any row sub-line — a `BookCell` label is the only way the string can be rendered.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { createElement } from "react";
import type { SourceStatus, TileProps } from "@/lib/framework/types";
import type { ManagedPortfolioConfig } from "./definition";

/** The window the tile is asked to answer. Mutable so one mock can serve every period. */
const host = vi.hoisted(() => ({ period: "1M" }));

vi.mock("@/lib/framework/store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  // `unit` IS part of the state the tile selects (#392). The real store always holds one — it is
  // seeded from `readUnit()` — so a mock omitting it does not model a reachable app state; it models
  // a tile reading `undefined`, which renders every figure as `—` and would fail this file for a
  // reason that has nothing to do with what it tests. `$` is the store's own default.
  useCockpitStore: (select: (s: unknown) => unknown) =>
    select({ period: host.period, unit: "$", openDetail: () => {}, openDetailForSymbol: () => {} }),
}));

vi.mock("@/lib/framework/instrument", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  // Prior closes are a different plane and a different defect (#298). An empty map keeps the day
  // line out of this test rather than letting it decide the outcome.
  useTodayRanges: () => new Map<string, number>(),
}));

import { ManagedPortfolioTile } from "./ManagedPortfolioTile";

import { renderedCells } from "./cellParser.fixture";
import { FRAME } from "./liveFrame.fixture";


function render(period: string, frame: unknown = FRAME): string {
  host.period = period;
  const props: TileProps<ManagedPortfolioConfig> = {
    instanceId: "portfolio-1",
    config: {},
    data: {
      trades: frame,
      account: { account: null },
      managers: { managers: [] },
      external_activity: { external: [] },
    },
    status: { trades: "live" as SourceStatus },
    onConfigChange: () => {},
  };
  // THE TILE NOW READS `/pnl/unrealized-base` for the per-lane window base (#699), so it needs a query
  // client — the same reason `bookCellHeadline.test.ts` already wraps its render. Retries off and no
  // refetching: this renders once, server-side, and a retrying query would race a network layer that
  // does not exist here. The query resolves to nothing, which is the correct "no base yet" state.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnMount: false } } });
  return renderToString(
    createElement(QueryClientProvider, { client: qc }, createElement(ManagedPortfolioTile, props)),
  );
}

describe("the fixture can express the defect", () => {
  // A fixture where the sweep carries nothing the cycles lack cannot fail, whatever the tile does.

  it("the SWEEP carries MANUAL-001 in the 1M window", () => {
    const by = (FRAME.realized_periods as Record<string, { by_strategy: Record<string, number> }>)["1M"]
      .by_strategy;
    expect(by["MANUAL-001"]).toBeCloseTo(1509.72, 2);
  });

  it("the live CYCLES do not — MANUAL-001 is flat", () => {
    const ids = new Set((FRAME.trades as Array<{ strategy_id: string }>).map((t) => t.strategy_id));
    expect(ids.has("MANUAL-001")).toBe(false);
    // ...and the fixture still has OTHER strategies, or `visibleBooks` returns [] for a single row
    // and every assertion below would pass over an empty panel.
    expect(ids.size).toBeGreaterThan(1);
  });

  it("the tile actually rendered its per-strategy panel", () => {
    // Guards the whole file against a render that silently produced nothing: if the panel is absent,
    // "MANUAL-001 is missing" is true for a reason that has nothing to do with #523.
    const html = render("1M");
    expect(html).toContain("MOMENTUM-002");
  });
});

describe("a FLAT strategy keeps its row on the default portfolio tile (#523)", () => {
  it("renders MANUAL-001 in the 1M window", () => {
    // THE DEFECT. +1509.72 realized, second largest in the book, rendered nowhere.
    expect(render("1M")).toContain("MANUAL-001");
  });

  it("renders MANUAL-001's SWEPT realized, not a confident zero", () => {
    // A ROW IS NOT THE FIX. Giving the flat strategy a row while its cell still reads the SESSION
    // figure renders `$0.00 · 0 held · real $0.00` — which asserts that the lane made nothing, when
    // the engine's own sweep says +1509.72. That is worse than the missing row: absent is unknown,
    // but $0.00 is a claim, and this repo has paid for that distinction repeatedly.
    //
    // This tile's local `BookCell` renders `fmtUsd(book.realized)` — the live-cycle sum — while
    // BookTile's cell takes `strategyPeriodRealized(tradesFrame, period, label)` (BookTile.tsx:459),
    // fixed under #345 item 3 and never applied here. Two cells, one question, different answers.
    const html = render("1M");
    const cell = html.slice(html.indexOf("MANUAL-001"));
    expect(cell).toContain("1,509.72");
  });

  it("renders MANUAL-001 in every window the sweep reports it in", () => {
    // Not just the one window from the report — 1W is a LOSS (-12.35) and must show too, or the
    // panel flatters the lane by dropping it exactly when it is down.
    for (const period of ["1W", "1M", "3M", "all"]) {
      expect(render(period), `period ${period}`).toContain("MANUAL-001");
    }
  });

  it("does NOT invent a row in 1D, where the sweep reports nothing closed", () => {
    // The sibling case. `realized_periods["1D"].by_strategy` is {} with closed_count 0 — a genuine
    // swept zero, not an unknown. A row there would assert a realization that did not happen.
    expect(render("1D")).not.toContain("MANUAL-001");
  });

  it("still does not give EXTERNAL a strategy row", () => {
    // EXTERNAL is in the sweep (-1093.30 on 3M/all) but is not a cockpit strategy; it belongs to the
    // unclaimed book. `visibleBooks` excludes it, and passing the map must not smuggle it back in.
    expect(render("all")).not.toContain("EXTERNAL");
  });
});

describe("the cells answer the WINDOW, not the session (#345 item 3)", () => {
  // Feeding the sweep into the cell changes what EVERY strategy displays, not just the flat one, so
  // the siblings need pinning too — otherwise the change is only tested where it happens to be visible.

  /** MOMENTUM-002's realized as the LIVE CYCLES see it: the session sum this tile used to print. */
  const sessionRealizedOfMomentum = (): number =>
    (FRAME.trades as Array<{ strategy_id: string; realized_pnl: string }>)
      .filter((t) => t.strategy_id === "MOMENTUM-002")
      .reduce((s, t) => s + Number.parseFloat(t.realized_pnl.split(" ")[0]), 0);

  it("the fixture's SESSION and WINDOW figures genuinely differ for MOMENTUM-002", () => {
    // If they matched, a cell still reading the session figure would render the right number anyway
    // and the assertion below would pass against the unfixed tile. Agreement is exactly the condition
    // under which a severed wire is invisible.
    const swept = (FRAME.realized_periods as Record<string, { by_strategy: Record<string, number> }>)["1M"]
      .by_strategy["MOMENTUM-002"];
    expect(swept).toBeCloseTo(3085.56, 2);
    expect(sessionRealizedOfMomentum()).not.toBeCloseTo(swept, 2);
  });

  it("a HELD strategy renders its swept window figure", () => {
    const cell = render("1M").slice(0, undefined);
    const i = cell.indexOf("MOMENTUM-002");
    expect(cell.slice(i, i + 400)).toContain("3,085.56");
  });

  it("...and NOT its session figure", () => {
    // The number the tile printed before this change. -8.29 is what `book.realized` sums to over the
    // live cycles, and it is what every cell showed under every period tab.
    const html = render("1M");
    const i = html.indexOf("MOMENTUM-002");
    expect(html.slice(i, i + 400)).not.toContain("8.29");
  });

  it("an UNSWEPT window renders an em dash, never a zero", () => {
    // THREE STATES, NEVER TWO. A frame whose sweep has not landed must not claim the lane made
    // nothing — that is the false-zero this whole change exists to avoid.
    const unswept = { ...(FRAME as Record<string, unknown>), realized_periods: null };
    const html = render("1M", unswept);
    const i = html.indexOf("MOMENTUM-002");
    const cell = html.slice(i, i + 400);
    expect(cell).toContain("—");
    expect(cell).not.toContain("$0.00");
  });
});

/**
 * Every rendered book cell as {label, realized}, straight out of the HTML.
 *
 * Reads what the OPERATOR reads. A test that sums the INPUTS to the panel could not have caught
 * this: the money was lost between the row list and the screen, not inside any function.
 *
 * THE HEADLINE, since #699 — that is where the window's realized now is. Before #699 the headline
 * was a standing level and this figure lived in the sub-line; reading the sub-line today finds it
 * only in the 1D-covered case, so this would silently resolve nothing and every sum assertion would
 * pass over an empty array. The em dash means "not swept" and is skipped, which cannot mask a
 * dropped row: a missing contributor breaks the sum either way.
 *
 * Parsing itself is shared (`cellParser.fixture`) — there were three copies of it and three
 * consecutive commits had to touch all three.
 */
function cellRealized(html: string): Array<{ label: string; realized: number }> {
  return renderedCells(html, "text-sm", "grid-cols-2 gap-px")
    .filter((c) => c.headline !== "—")
    .map((c) => ({ label: c.label, realized: Number(c.headline.replace(/[$,]/g, "")) }));
}

describe("the rows must SUM to the engine's total — even with no live unclaimed position (#596, one row over)", () => {
  // WHAT THE REVIEW FOUND. `visibleBooks` drops EXTERNAL from the flat rows because "it already has
  // its own Unclaimed row" — but that row is only emitted when `speaks(a.unclaimed)`, i.e. when there
  // is a LIVE external POSITION. With none, EXTERNAL's swept money is dropped by the filter and never
  // picked up by anything: five cells summed to 4,371.87 against an engine total of 3,278.71 and the
  // missing -1,093.16 appeared NOWHERE in the HTML. The book reads ~$1,093 better than it is.
  //
  // This is exactly the failure this branch already refused once: not a missing number, a WRONG one,
  // on the default screen. The earlier test pinned only that EXTERNAL gets no strategy row — it never
  // asked where the money went.

  const ENGINE_TOTAL = 3278.71;

  it("the fixture HAS unclaimed money and NO live unclaimed position", () => {
    // Both halves are load-bearing. With a live external position the Unclaimed row appears anyway and
    // the bug is invisible; with a zero sweep there is nothing to lose.
    const row = (FRAME.realized_periods as Record<string, { by_strategy: Record<string, number>; unclaimed: number; total: number }>)
      .all;
    expect(row.by_strategy.EXTERNAL).toBeCloseTo(-1093.3, 2);
    expect(row.unclaimed).toBeCloseTo(0.14, 2);
    expect(row.total).toBeCloseTo(ENGINE_TOTAL, 2);
    // The render below passes `external_activity: { external: [] }` — no live unclaimed position.
  });

  it("the parser actually reads the panel", () => {
    // If `renderedCells` returned nothing, every sum assertion would pass over an empty array.
    const cells = cellRealized(render("all"));
    expect(cells.length).toBeGreaterThan(3);
    expect(cells.map((c) => c.label)).toContain("MOMENTUM-002");
  });

  it("an Unclaimed row is rendered to carry the swept external money", () => {
    expect(cellRealized(render("all")).map((c) => c.label)).toContain("Unclaimed");
  });

  it("the rendered cells sum to the engine's own total", () => {
    // THE INVARIANT THE PANEL EXISTS TO SATISFY. Sum(rows) === total, read off the screen.
    const sum = cellRealized(render("all")).reduce((a, c) => a + c.realized, 0);
    expect(sum).toBeCloseTo(ENGINE_TOTAL, 1);
  });

  it("and the missing figure is actually ON the screen", () => {
    // Named explicitly: EXTERNAL -1093.30 + residual 0.14. A sum that happens to balance because two
    // errors cancel would still pass the assertion above.
    expect(render("all")).toContain("1,093.16");
  });
});
