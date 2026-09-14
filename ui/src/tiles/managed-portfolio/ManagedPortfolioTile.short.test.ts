/**
 * #855 — the managed portfolio row prints the day's dollars with the side and the day's percent
 * without it.
 *
 *     const day    = dayResult.covered > 0 ? dayResult.value : null;   // :325, via dayMove — SIGNED
 *     const dayPct = ... ((group.last - group.priorClose) / group.priorClose) * 100;  // :335 — UNSIGNED
 *
 * `dayMove` gets this right, deliberately and with a comment saying so (`books.ts:423-425`: "Signed
 * quantity: a short gains when the price falls"). The percent three lines away is the symbol's move,
 * not the position's, so a short whose mark rose renders `day -$100.00 (+10.00%)` — the two halves of
 * one cell disagreeing about which way today went.
 *
 * The percent beside it already carries a hard-won basis rule: it is WITHHELD entirely when any cycle
 * in the row opened today, because a prior-close percent beside entry-based dollars contradicts itself
 * (an NBIS-style row read "+$46" beside "+29%"). That is the same defect this is — the pair must agree
 * — caught once for the basis and missed for the side.
 *
 * DRIVEN THROUGH THE EXPORTED TILE, not through a copy of the expression. `PortfolioRow` is private
 * and the two numbers are computed inline in its body; recomputing them in a test would assert that my
 * arithmetic matches my arithmetic. `react-dom/server` renders the real component tree, so what is
 * asserted is the markup the operator reads.
 *
 * The three hooks that reach the WS/REST planes are stubbed at the module boundary with the shapes
 * they actually return — `useTodayRanges` a `Map<instrument_id, prev_close>`, `useBars` a `BarDTO[]`,
 * `useInstrument` the `InstrumentData` record. Everything between the frame and the markup is real.
 */
import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { TradeDTO } from "@/lib/api/types";
import { deployedValue, type ExternalLike } from "./books";

const PRIOR_CLOSE = 100;
const MARK = 110;

vi.mock("@/lib/framework/instrument", async (importActual) => {
  const actual = await (importActual as () => Promise<Record<string, unknown>>)();
  return {
    ...actual,
    useTodayRanges: () => new Map<string, number>([["AAPL.XNAS", PRIOR_CLOSE]]),
    useBars: () => [],
    useInstrument: () => ({
      bars: [],
      price: MARK,
      quote: null,
      vwap: null,
      todayRange: { high: 111, low: 99, prevClose: PRIOR_CLOSE },
      fundamentals: null,
      fills: [],
      status: "live",
    }),
  };
});

const { ManagedPortfolioTile } = await import("./ManagedPortfolioTile");

/** A managed cycle as `/trades` emits it: UNSIGNED `quantity`, SIGNED `market_value`/`unrealized_pl`.
 *  `opened_ts: 0` keeps the cycle OFF today, so the percent is not withheld by the same-day rule. */
const cycle = (over: Partial<TradeDTO>): TradeDTO =>
  ({
    account_id: "DU1",
    client_id: "IB",
    instrument_id: "AAPL.XNAS",
    strategy_id: "MOMENTUM-002",
    cycle_id: "c1",
    state: "HELD",
    side: "LONG",
    quantity: 10,
    is_capital_deployed: true,
    is_engaged: true,
    avg_px_open: PRIOR_CLOSE,
    realized_pnl: "0.00 USD",
    last_px: MARK,
    market_value: 1100,
    unrealized_pl: 100,
    leg_count: 1,
    opened_ts: 0,
    last_event_ts: 0,
    working_orders: [],
    ...over,
  }) as TradeDTO;

/** Equity chosen so a correctly-valued pair of shorts is exactly 10% deployed: 2 x 10 x 110 / 22000. */
const EQUITY = 22_000;

function renderTile(
  trades: TradeDTO[],
  account: { equity: number } | null = null,
  external: ExternalLike[] = [],
): string {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const html = renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: qc } as never,
      createElement(ManagedPortfolioTile as never, {
        instanceId: "i",
        config: {},
        onConfigChange: () => {},
        data: {
          trades: { trades },
          account: { account },
          managers: { managers: [] },
          external_activity: { external: external.map((x) => ({ ...x, source: "POSITION" })) },
        },
        status: { trades: "live", account: "live", managers: "live", external_activity: "live" },
      } as never),
    ),
  );
  return html.replace(/<[^>]+>/g, "|").replace(/&amp;/g, "&").replace(/\|+/g, "|");
}

const renderRow = (t: TradeDTO): string => renderTile([t]);

/** The row's Net cell — the one between the strategy tag and the average cost. */
function netCell(text: string): string {
  const m = text.match(/\|AAPL\|MOMENTUM\|([^|]*)\|/);
  return m ? m[1].trim() : "";
}

/** The `· NN%` deployed reading in the tile header, or "" when no equity was supplied.
 *  The minus is part of the pattern DELIBERATELY: a negative reading is the defect this file is
 *  about, and an anchor that could not match one returned "" — which reads as "no equity supplied"
 *  rather than as "the header says the account is MINUS five percent deployed". */
function deployedPct(text: string): string {
  const m = text.match(/held · (-?\d+%\+?)/);
  return m ? m[1] : "";
}

/** The `· day -$100.00 (+10.00%)` fragment of the row's sub-line. */
function dayCell(text: string): string {
  const m = text.match(/day -?\$[\d,.]+ \([-+][\d.]+%\)/);
  return m ? m[0] : "";
}

const SHORT = cycle({ side: "SHORT", quantity: 10, market_value: -1100, unrealized_pl: -100 });

describe("the fixture can express the bug", () => {
  it("the row is a SHORT and BOTH numbers of the day cell are rendered", () => {
    // Vacuity guard, read off the RENDER. Three separate conditions can silently empty this cell —
    // no prior close on the today_ranges plane, no mark, or a cycle opened today (which withholds the
    // percent by design). Any of them and a sign assertion below would pass against an absent number.
    const text = renderRow(SHORT);
    expect(text).toContain("|AAPL|");
    expect(text).toContain("|-10|"); // netQty: the row knows it is short
    expect(dayCell(text)).not.toBe("");
    expect(MARK).toBeGreaterThan(PRIOR_CLOSE); // the day went AGAINST this short
  });
});

describe("the row's day dollars and day percent describe the same day (#855)", () => {
  it("a short whose mark rose above the prior close is DOWN in both halves of the cell", () => {
    // 10 short at a prior close of 100, marked 110: `dayMove` says −$100 and it is right. The percent
    // is `(110 − 100) / 100` — the SYMBOL's day, printed on a POSITION's row, with the position's
    // direction dropped.
    const cell = dayCell(renderRow(SHORT));
    expect(cell).toContain("-$100.00"); // the dollars are already right
    expect(cell).not.toContain("(+10.00%)"); // the percent must not contradict them
    expect(cell).toContain("(-10.00%)");
  });

  it("a long whose mark rose is UP in both halves — the pair only breaks on a short", () => {
    // The sibling that passes, so the failure above points at the side, not at the cell.
    const cell = dayCell(renderRow(cycle({})));
    expect(cell).toContain("$100.00");
    expect(cell).toContain("(+10.00%)");
  });
});

/**
 * The mixed-spelling row: two shorts of 10 in one position, one signed and one not.
 *
 * `grouping.ts:77` applies the side to a quantity it has not normalised, so the two legs come out
 * -10 and +10 and the row nets to 0. That number is then read twice more, and both readings go quiet
 * rather than wrong:
 *
 *   `:311` colours the Net cell — `> 0` bull, `< 0` bear, otherwise neutral. MEASURED: the cell does
 *          not render "0", it renders the WORD "flat". A row holding 20 short shares states in
 *          English that it holds none.
 *   `:555` values the book — `Math.abs(g.netQty) * g.last` — so the position contributes NOTHING to
 *          the deployed reading, and the header says the account is 0% deployed while it is short
 *          $2,200 of stock.
 *
 * A wrong number invites a second look. "flat · 0%" does not: it agrees with an empty book, which is
 * the state the operator is least likely to question. This is the render-level case for the unit
 * failure pinned in `grouping.short.test.ts`.
 */
describe("a mixed signed/unsigned pair of shorts is one 20-share position (#855)", () => {
  const PAIR = [
    cycle({ cycle_id: "c1", side: "SHORT", quantity: 10, market_value: -1100, unrealized_pl: -100 }),
    cycle({ cycle_id: "c2", side: "SHORT", quantity: -10, market_value: -1100, unrealized_pl: -100 }),
  ];

  it("the fixture is one ROW of two cycles, and the tile renders it", () => {
    // Vacuity guard. The two cycles must share instrument AND strategy or `groupByPosition` emits two
    // rows, netQty is never summed, and the cancellation this test exists for cannot happen.
    expect(PAIR[0].instrument_id).toBe(PAIR[1].instrument_id);
    expect(PAIR[0].strategy_id).toBe(PAIR[1].strategy_id);
    expect(PAIR[0].cycle_id).not.toBe(PAIR[1].cycle_id);
    const text = renderTile(PAIR, { equity: EQUITY });
    expect(text).toContain("|AAPL|");
    expect(text).toContain("2 held");
    expect(deployedPct(text)).not.toBe(""); // equity arrived; the header can report a percentage
  });

  it("the Net cell reads -20, not 0", () => {
    expect(netCell(renderTile(PAIR, { equity: EQUITY }))).toBe("-20");
  });

  it("the position still counts toward capital deployed", () => {
    // 20 shares at 110 against $22,000 of equity is 10%. Reading 0% says the account is holding
    // nothing while it is short $2,200 of stock.
    expect(deployedPct(renderTile(PAIR, { equity: EQUITY }))).toBe("10%");
  });

  it("two unsigned shorts already net to -20 — the pair is what breaks, not the sum", () => {
    // The sibling that passes today, so the failures above point at the spelling rather than at
    // multi-cycle rows in general.
    const unsigned = [
      cycle({ cycle_id: "c1", side: "SHORT", quantity: 10, market_value: -1100, unrealized_pl: -100 }),
      cycle({ cycle_id: "c2", side: "SHORT", quantity: 10, market_value: -1100, unrealized_pl: -100 }),
    ];
    expect(netCell(renderTile(unsigned, { equity: EQUITY }))).toBe("-20");
    expect(deployedPct(renderTile(unsigned, { equity: EQUITY }))).toBe("10%");
  });
});


/**
 * The header's deployed reading is the SAME reading as `deployedValue`'s (#855).
 *
 * `heldValue`/`pctDeployed` were a THIRD derivation of deployed capital, and the fix that repaired
 * `deployedValue`'s two branches left it behind: its managed half took `|netQty| × last`, the
 * committed-capital convention, while its unclaimed half took the row's raw `market_value`. So an
 * unclaimed short SUBTRACTED from the header while the identical row ADDED in `deployedValue` — the
 * same disagreement, one level up, on the number the operator actually reads.
 *
 * These assert the two ANSWER THE SAME, and assert the value as well, because "they agree" is
 * satisfiable by breaking both.
 */
describe("the header's deployed figure is deployedValue's (#855)", () => {
  /** An unclaimed broker row as `/external_activity` emits it — the tile reads `instrument_id`,
   *  `side`, `avg_px` and `last_px` too, and a double missing them cannot be rendered at all. */
  const SHORT_UNCLAIMED = {
    instrument_id: "AAPL.XNAS",
    strategy_id: "EXTERNAL",
    origin: "VENUE",
    side: "SHORT",
    quantity: 10,
    avg_px: 100,
    last_px: MARK,
    realized_pnl: "0.00 USD",
    unrealized_pl: -100,
    market_value: -1100,
    venue_qty: -10,
    ts_last: 0,
  } as unknown as ExternalLike;

  it("the fixture is an unclaimed SHORT the sums must not skip", () => {
    // Vacuity guard on both halves: a positive market value would agree by accident, and a phantom
    // (`venue_qty === 0`) is excluded from every sum, so it could not disagree either.
    expect(SHORT_UNCLAIMED.market_value).toBeLessThan(0);
    expect(SHORT_UNCLAIMED.venue_qty).not.toBe(0);
    expect(deployedValue([], [SHORT_UNCLAIMED]).value).toBe(1100); // the oracle it must match
  });

  it("an unclaimed short ADDS to the header, the way it adds to deployedValue", () => {
    // $1,100 of committed capital against $22,000 of equity is 5%. Reading 0% — or worse, a figure
    // reduced by the position — says the account has more room free because it took on more risk.
    expect(deployedPct(renderTile([], { equity: EQUITY }, [SHORT_UNCLAIMED]))).toBe("5%");
  });

  it("a managed short and an unclaimed short of the same size read the same in the header", () => {
    // The two-derivations check at the surface, not in the helper.
    const managed = deployedPct(renderTile([SHORT], { equity: EQUITY }));
    const unclaimed = deployedPct(renderTile([], { equity: EQUITY }, [SHORT_UNCLAIMED]));
    expect(managed).toBe("5%");
    expect(unclaimed).toBe(managed);
  });
});


/**
 * The row's day percent takes its sign from the ROW, not from whether the dollars resolved (#855).
 *
 * The first version of this fix read `Math.sign(day)`, which also made the percent's EXISTENCE depend
 * on `day`. MEASURED, and it is why this block asserts an agreement rather than a restoration: that
 * clause was DEAD, not a regression. `grouping.ts` sets `group.last` only inside
 * `if (t.is_capital_deployed)` and only from a non-null `last_px`, and `books.ts::dayMove` counts a
 * cycle as covered under the identical two conditions with the same prior close as its basis — so
 * `group.last != null && group.priorClose` already implies `day != null`, and the extra clause could
 * never be the deciding one. A test for the case the review described could not fail, because the
 * fixture cannot reach it: with no `last_px` neither version renders a percent.
 *
 * What IS worth pinning is that the two readings of one direction agree. `netQty` is the honest
 * source — under NETTING a group is one instrument and one strategy, so it is single-sided — and it
 * is available whether or not the dollars are.
 */
describe("the day percent and the day dollars agree on direction", () => {
  it("a short: both negative, and the sign matches netQty", () => {
    const text = renderTile([SHORT], { equity: EQUITY });
    const cell = dayCell(text);
    expect(cell).toContain("-$100.00");
    expect(cell).toContain("(-10.00%)");
    expect(netCell(text)).toBe("-10"); // the row's own direction, the source of the percent's sign
  });

  it("a long: both positive, and the sign matches netQty", () => {
    const text = renderTile([cycle({})], { equity: EQUITY });
    const cell = dayCell(text);
    expect(cell).toContain("$100.00");
    expect(cell).toContain("(+10.00%)");
    expect(netCell(text)).toBe("10");
  });
});


/**
 * The unclaimed row renders a share COUNT, like every other quantity on this board (#855).
 *
 * The decode absolutises this field, so on a live stack the raw read was harmless — which is exactly
 * why it survived: no test hands a tile a row that did not come through a decode, except the ones
 * that render components directly, and this cell had none. `normaliseQuantity.ts` states the rule the
 * other four render sites already followed: the decode is the class-closer, the render sites are belt
 * and braces, and they have to be consistent with each other or a reader cannot tell an oversight
 * from a deliberate reliance.
 */
describe("an unclaimed row's quantity is a share count", () => {
  const signedUnclaimed = {
    instrument_id: "AAPL.XNAS",
    strategy_id: "EXTERNAL",
    origin: "VENUE",
    side: "SHORT",
    quantity: -10,
    avg_px: 100,
    last_px: MARK,
    realized_pnl: "0.00 USD",
    unrealized_pl: -100,
    market_value: -1100,
    venue_qty: -10,
    ts_last: 0,
  } as unknown as ExternalLike;

  /** The quantity cell of the UNCLAIMED table row — the one right after the symbol. */
  const unclaimedQty = (text: string) => text.match(/\|AAPL\|(-?[\d.]+)\|/)?.[1] ?? "";

  it("the fixture is a signed short and the row renders at all", () => {
    // Vacuity guard on both halves: an unsigned fixture could not violate this, and a phantom row
    // (`venue_qty === 0`) is still listed but would make the sums assertions elsewhere misleading.
    expect((signedUnclaimed as { quantity: number }).quantity).toBe(-10);
    expect(unclaimedQty(renderTile([], null, [signedUnclaimed]))).not.toBe("");
  });

  it("renders 10, not -10", () => {
    expect(unclaimedQty(renderTile([], null, [signedUnclaimed]))).toBe("10");
  });
});
