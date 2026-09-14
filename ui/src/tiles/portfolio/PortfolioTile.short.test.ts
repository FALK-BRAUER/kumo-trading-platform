/**
 * #855 — the portfolio row hands a SHORT position to a LONG-only recommender and prints the answer.
 *
 *     const rec = recommend(levels, pct);          // PortfolioTile.tsx:32
 *
 * `recommend` takes levels and a P&L percent. It takes no side, and every branch reads as a long:
 * "below the cloud — trend broken" -> EXIT, "below Kijun — momentum fading" -> WATCH, "above cloud —
 * raise stop, lock gains" -> TRAIL. For a short, price below the cloud is the thesis WORKING, and EXIT
 * is the single most expensive thing this cell can say: it tells the operator to close the position
 * that is winning, on the day it started winning.
 *
 * The ticket's answer is REFUSAL, not mirroring. Mirroring would require deciding that every one of
 * those five branches inverts cleanly for a short, which is a trading claim nobody has made and which
 * is not obviously true (Ichimoku's cloud is not symmetric in how it is used). A cell that says
 * nothing is honest about a question this function was never built to answer; a mirrored cell is a
 * new, untested opinion wearing the same badge as the old one. Three states, not two — LONG's answer,
 * a refusal, and never a guess.
 *
 * DRIVEN THROUGH THE EXPORTED TILE. `PositionRow` is private, so the assertion is on the rendered
 * signal cell — which is also where a fix has to land, since `recommend` cannot refuse for a side it
 * is never told.
 */
import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { lastLevels, recommend } from "@/lib/ichimoku";
import type { BarDTO, PositionDTO } from "@/lib/api/types";

/** 60 sessions stepping steadily DOWN. Enough for spanB (52) and low enough at the end that the last
 *  close sits under the cloud — the branch that returns EXIT. */
const DOWNTREND: BarDTO[] = Array.from({ length: 60 }, (_, i) => {
  const close = 200 - i * 2; // 200 -> 82
  return {
    instrument_id: "AAPL.XNAS",
    ts_event: i,
    open: close,
    high: close + 1,
    low: close - 1,
    close,
    volume: 1_000,
  } as BarDTO;
});

const LAST_CLOSE = DOWNTREND[DOWNTREND.length - 1].close;

vi.mock("@/lib/framework/instrument", async (importActual) => {
  const actual = await (importActual as () => Promise<Record<string, unknown>>)();
  return {
    ...actual,
    useBars: () => DOWNTREND,
    useInstrument: () => ({
      bars: DOWNTREND,
      price: LAST_CLOSE,
      quote: null,
      vwap: null,
      todayRange: null,
      fundamentals: null,
      fills: [],
      status: "live",
    }),
  };
});

const { PortfolioTile } = await import("./PortfolioTile");

/** What `/positions` emits: UNSIGNED quantity, direction in `side`. Entry ABOVE the last close, so a
 *  short is IN PROFIT here and a long is in loss — the fixture cannot be read as side-agnostic. */
const position = (over: Partial<PositionDTO>): PositionDTO =>
  ({
    instrument_id: "AAPL.XNAS",
    side: "LONG",
    quantity: 10,
    avg_px_open: 190,
    realized_pnl: "0.00 USD",
    strategy_id: "MOMENTUM-002",
    ts_last: 0,
    ...over,
  }) as PositionDTO;

function renderTile(p: PositionDTO): string {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const html = renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: qc } as never,
      createElement(PortfolioTile as never, {
        instanceId: "i",
        config: {},
        onConfigChange: () => {},
        data: { positions: { positions: [p] } },
        status: { positions: "live" },
      } as never),
    ),
  );
  return html.replace(/<[^>]+>/g, "|").replace(/&amp;/g, "&").replace(/\|+/g, "|");
}

/** The row's signal cell — the one between the symbol and the side. Extracted rather than searched
 *  for across the whole markup, so an unrelated "HOLD" elsewhere on the tile cannot answer for it. */
// The side cell is THREE-state (LONG | SHORT | FLAT), so the anchor is too — a two-state anchor
// silently returned "" for a flat row, which reads as "no signal" rather than as "unmatched".
const SIDE = "(?:LONG|SHORT|FLAT)";

function signalCell(text: string): string {
  const m = text.match(new RegExp(`\\|AAPL\\|([^|]*)\\|${SIDE}\\|`));
  return m ? m[1].trim() : "";
}

/**
 * The row is `| symbol | signal | side  qty | last | P&L |`. Both helpers below anchor on that shape
 * from the symbol cell forward, for the same reason `signalCell` does: a bare value match would
 * accept a number from the sub-line (`avg 190.00 · real 0.00 USD`) or from a header.
 */
function qtyCell(text: string): string {
  const m = text.match(new RegExp(`\\|AAPL\\|[^|]*\\|${SIDE}\\| \\|(-?[\\d.]+)\\|`));
  return m ? m[1] : "";
}
function pnlCell(text: string): string {
  const m = text.match(new RegExp(`\\|AAPL\\|[^|]*\\|${SIDE}\\| \\|-?[\\d.]+\\|[^|]*\\|([^|]*)\\|`));
  return m ? m[1].trim() : "";
}

/**
 * EVERY status `recommend` can return (`ichimoku.ts:108-122`). Banning only EXIT is not a test: the
 * same defect returns WATCH the moment the mark sits between Kijun and the cloud, and TRAIL or ADD
 * once a short's P&L percent goes positive — all four are long readings, and HOLD ("thesis intact")
 * is one too. The refusal has to be outside the whole vocabulary, not outside its worst member.
 */
const LONG_VOCABULARY = ["EXIT", "WATCH", "TRAIL", "ADD", "HOLD"];

/**
 * The label the fix will render instead. CHOSEN HERE, deliberately, rather than left open: a test
 * that only says "not one of those five" goes green on an empty cell, and an empty cell is the
 * silent-degradation this repo keeps paying for — the operator cannot tell "no opinion" from "the
 * signal plane is down". Three states, not two: a long reading, an explicit refusal, never a blank.
 */
const REFUSAL = "REFUSED · short";

describe("the fixture can express the bug", () => {
  it("these bars genuinely reach the EXIT branch, and a LONG row prints it", () => {
    // Vacuity guard in two parts. First on the levels themselves: if the series were too short for a
    // cloud, `lastLevels` returns null and `recommend` answers HOLD/"awaiting price data" for every
    // side — a refusal by accident, and every assertion below would pass on a dead fixture.
    const levels = lastLevels(DOWNTREND);
    expect(levels).not.toBeNull();
    expect(levels?.cloudBot).not.toBeNull();
    expect(levels!.price).toBeLessThan(levels!.cloudBot!);
    expect(recommend(levels, -50).status).toBe("EXIT");

    // Then on the render: the tile really does put that status in the row.
    expect(renderTile(position({}))).toContain("|EXIT|");
  });

  it("the short fixture is a short, and it is WINNING", () => {
    const p = position({ side: "SHORT" });
    expect(p.side).toBe("SHORT");
    expect(p.quantity).toBeGreaterThan(0); // the wire contract: UNSIGNED
    expect(LAST_CLOSE).toBeLessThan(p.avg_px_open); // sold at 190, marked at 82
  });
});

describe("a LONG-only read is not offered for a SHORT (#855)", () => {
  it("the short row's signal is outside the ENTIRE long vocabulary, not just outside EXIT", () => {
    // Same bars, same levels, opposite side. Today both rows print EXIT — the tile cannot tell them
    // apart because `recommend` is never told which it is looking at. Banning EXIT alone would let a
    // fix that returns WATCH or HOLD for a short go green while still handing a long reading to a
    // short position.
    const cell = signalCell(renderTile(position({ side: "SHORT" })));
    expect(cell).not.toBe(""); // an empty cell is not a refusal
    expect(LONG_VOCABULARY).not.toContain(cell);
  });

  it("the short row says WHY it has nothing to say", () => {
    // The label is pinned so the refusal is a state the operator can read, not an absence. See the
    // note on REFUSAL above for why this test names a word rather than leaving the cell open.
    expect(signalCell(renderTile(position({ side: "SHORT" })))).toBe(REFUSAL);
  });

  it("`recommend` refuses rather than mirrors when told the position is short", () => {
    // The seam a fix has to open: the side must reach `recommend`, and the answer for a short must be
    // an abstention — not the long verdict, and not its inverse. Mirroring would assert that all five
    // branches invert cleanly for a short, which is a trading claim nobody has made.
    const levels = lastLevels(DOWNTREND);
    const rec = (recommend as (l: typeof levels, p: number, side?: string) => { status: string })(
      levels,
      -50,
      "SHORT",
    );
    expect(LONG_VOCABULARY).not.toContain(rec.status);
  });

  it("every long-vocabulary status is genuinely reachable, so banning them means something", () => {
    // Vacuity guard on the ban list. A vocabulary containing words `recommend` can never return would
    // make the assertions above weaker than they look — the ban would be over words nothing says.
    // Levels are built to land on each branch in turn; HOLD is reachable two ways and one is enough.
    const L = (over: Record<string, number | null>) =>
      ({ price: 100, tenkan: 90, kijun: 80, spanA: 70, spanB: 60, cloudTop: 70, cloudBot: 60, ma200: null, ...over }) as never;
    const said = new Set([
      recommend(L({ price: 50 }), 0).status, // below the cloud
      recommend(L({ price: 75 }), 0).status, // below Kijun
      recommend(L({ price: 100 }), 25).status, // above cloud, +25%
      recommend(L({ price: 95, tenkan: 90, cloudTop: 200 }), 10).status, // above Tenkan, +10%
      recommend(L({ price: 100 }), 0).status, // above cloud, flat
    ]);
    for (const status of LONG_VOCABULARY.filter((s) => s !== "HOLD")) expect([...said]).toContain(status);
    expect([...said]).toContain("HOLD");
  });

  it("the row reads the same whichever way the quantity is spelled", () => {
    // The paired-spelling case at the seam. `computePnl` feeds this cell, so the same double
    // negation pinned in `instrument.short.test.ts` surfaces here as a row that reports a short of
    // 10 as +$1,080 and the identical short spelled -10 as -$1,080.
    //
    // Sold at 190, marked at 82: the short is UP $1,080, which is 56.8% of the $1,900 committed.
    // ANCHORED ON THE ROW'S CELLS, like `signalCell` above. An unanchored dollar-and-percent match
    // would accept any such pair anywhere in the tile — the sub-line carries `avg` and `real`, and a
    // header could grow one — so it could go green on a number from a different column.
    expect(pnlCell(renderTile(position({ side: "SHORT", quantity: 10 })))).toBe("+$1,080 +56.8%");
    expect(pnlCell(renderTile(position({ side: "SHORT", quantity: -10 })))).toBe("+$1,080 +56.8%");
  });

  it("the quantity column shows a share count, not a signed one", () => {
    // The side already has its own column right beside it. Printing "SHORT -10" says the position is
    // short negative ten shares, which is a long.
    expect(qtyCell(renderTile(position({ side: "SHORT", quantity: 10 })))).toBe("10");
    expect(qtyCell(renderTile(position({ side: "SHORT", quantity: -10 })))).toBe("10");
  });

  it("a FLAT row is neither long nor short, so it gets no reading either", () => {
    // `PositionDTO.side` is documented `LONG | SHORT | FLAT` (`backend/api/models.py:109`). A FLAT row
    // held nothing to recommend on, and the tile passed it to the long-only recommender and then
    // rendered its badge as "SHORT" via `long ? "LONG" : "SHORT"` — a two-state read of a three-state
    // field, which is this repo's most-repeated defect wearing a new hat.
    const text = renderTile(position({ side: "FLAT", quantity: 0 }));
    expect(LONG_VOCABULARY).not.toContain(signalCell(text));
    expect(signalCell(text)).not.toBe(""); // an empty cell is not an answer either
    expect(text).toContain("|FLAT|");
    expect(text).not.toContain("|SHORT|");
  });

  it("the long row is unchanged — the refusal is scoped to shorts", () => {
    // The sibling. A fix that makes `recommend` refuse whenever it is handed a side would silence the
    // cell for the whole book, which is a bigger regression than the bug.
    expect(renderTile(position({ side: "LONG" }))).toContain("|EXIT|");
  });
});
