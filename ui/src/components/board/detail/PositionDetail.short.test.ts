/**
 * #855 — the position detail surface reports a SHORT as if it were a long, twice.
 *
 * Driven through `PositionDetailView`, the exported presentational half, rendered for real with
 * `react-dom/server`. Not through the private lambdas: the two defects are in expressions inside the
 * component body, and a test that recomputes them beside the component is a second copy of the bug,
 * not a check on it.
 *
 * (Why `createElement` and a `.ts` extension rather than JSX in a `.tsx`: `vitest.config.ts` collects
 * `src/**\/*.test.ts` only, and there is no jsdom or Testing Library in this repo. `react-dom/server`
 * renders in the node environment as-is, so the rendered MARKUP — what the operator actually reads —
 * is assertable without adding a DOM stack or widening the include glob.)
 *
 * The two defects, both visible in one render of `SHORT 10 @ 100, mark 110, prior close 100`:
 *
 *   1. `mktValue = price * p.quantity` (:246). `quantity` is unsigned on the wire, so a short reports
 *      `$1,100` of market value. The engine signs this everywhere else — `engine_node.py:7441`
 *      (`d.market_value = last * signed`) and `:7515` — and it signs it BECAUSE the unsigned form
 *      already shipped: WHD held long 28 and short 28 across two strategies, genuinely flat, and the
 *      two legs summed to +$3,849 of exposure that did not exist. This surface is the third path.
 *   2. `dayAmt` (:249) uses the signed-quantity form; `dayPct` (:250) does not. The rendered row reads
 *      `−$100  +10.00%` — one number saying the day went against the position and the one beside it
 *      saying it went for it.
 */
import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { PositionDetailView } from "./PositionDetail";
import type { PositionDTO } from "@/lib/api/types";

/** What `/positions` emits: UNSIGNED `quantity`, direction in `side`, realized as a Money string. */
const position = (over: Partial<PositionDTO>): PositionDTO =>
  ({
    instrument_id: "AAPL.XNAS",
    side: "LONG",
    quantity: 10,
    avg_px_open: 100,
    realized_pnl: "0.00 USD",
    strategy_id: "MANUAL-001",
    ts_last: 0,
    ...over,
  }) as PositionDTO;

/** Rendered markup, stripped to its text. Entities decoded so `&minus;`/`−` compare as written. */
function renderText(p: PositionDTO, price: number | null, priorClose: number | null): string {
  const html = renderToStaticMarkup(
    createElement(PositionDetailView as never, { position: p, price, priorClose } as never),
  );
  return html
    .replace(/<[^>]+>/g, "|")
    .replace(/&amp;/g, "&")
    .replace(/&#x27;/g, "'")
    .replace(/\|+/g, "|");
}

/** The rendered `Day` cell: `|−$100|+10.00%|` between the `Day` label and the next `Row`. */
function daySection(text: string): string {
  const i = text.indexOf("Day|");
  const j = text.indexOf("Market value", i);
  return text.slice(i, j);
}

/**
 * The rendered market value AS A NUMBER, whichever way the minus is written.
 *
 * THE SIGN IS THIS FILE'S SUBJECT; THE GLYPH IS NOT. Asserting on the string `−$1,100` (U+2212) made
 * the test fail on `$-1,100` — a rendering whose sign is already correct — and would have moved on a
 * pure formatter change that touched no arithmetic. Two questions, two tests: this file owns "which
 * way does the number point", and the formatter's own test owns "how is a negative dollar written".
 */
function marketValue(text: string): number | null {
  const m = text.match(/Market value\|(−|-)?\$(−|-)?([\d,]+)/);
  if (!m) return null;
  const negative = Boolean(m[1] || m[2]);
  return (negative ? -1 : 1) * Number(m[3].replace(/,/g, ""));
}

const SHORT = position({ side: "SHORT", quantity: 10, avg_px_open: 100 });

describe("the fixture can express the bug", () => {
  it("the rendered surface is a SHORT whose mark has risen above the prior close", () => {
    // Vacuity guard, and it has to be read off the RENDER, not off the fixture object: if the view
    // never reached the SHORT branch (a missing price short-circuits both `mktValue` and `dayAmt` to
    // "—") every sign assertion below would pass against an em-dash.
    const text = renderText(SHORT, 110, 100);
    expect(text).toContain("|SHORT|");
    expect(text).toContain("|10 @ 100.00|"); // the unsigned wire quantity, rendered
    expect(text).toContain("|Mark|110.00|"); // 110 > 100: the day went AGAINST this short
    expect(daySection(text)).not.toContain("—");
  });
});

describe("market value carries the side (#855)", () => {
  it("a short's market value is NEGATIVE, the way the engine signs it", () => {
    // `books.ts:423-425` states the convention this repo settled on — "Signed quantity: a short gains
    // when the price falls" — and `engine_node.py:7441` applies it to `market_value` itself. Short 10
    // marked at 110 is a $1,100 LIABILITY. Rendering `$1,100` puts it on the wrong side of the
    // account: a book that is net flat reads as fully deployed.
    expect(marketValue(renderText(SHORT, 110, 100))).toBe(-1100);
  });

  it("a long of the same size still reports a POSITIVE market value", () => {
    // The sibling. A fix that signs by `quantity` alone, or that abs()es everything, breaks this one.
    expect(marketValue(renderText(position({ side: "LONG", quantity: 10 }), 110, 100))).toBe(1100);
  });
});

describe("the day amount and the day percent describe the same day (#855)", () => {
  it("a short whose mark rose shows a LOSS in both cells, not a loss and a gain", () => {
    // `dayAmt` = signedQty x (mark − prior) = −10 x (110 − 100) = −$100. The percent beside it is the
    // symbol's move, +10%, printed without the position's side. The pair contradicts itself on the
    // one row the operator reads to decide whether today hurt.
    const day = daySection(renderText(SHORT, 110, 100));
    expect(day).toContain("−$100"); // the dollars are already right
    expect(day).not.toContain("+10.00%"); // the percent must not disagree with them
    expect(day).toContain("-10.00%");
  });

  it("a long whose mark rose shows a GAIN in both cells", () => {
    // The sibling, so a fix cannot simply negate every percent.
    const day = daySection(renderText(position({ side: "LONG", quantity: 10 }), 110, 100));
    expect(day).toContain("+$100");
    expect(day).toContain("+10.00%");
  });
});

/**
 * The same position written both ways, through the same render.
 *
 * This surface holds both forms of the bug at once, which makes it the clearest place to show why
 * agreement between spellings is the property worth pinning:
 *
 *   `mktValue = price * p.quantity`  — unsigned quantity gives +$1,100, signed gives −$1,100. The
 *                                      SIGNED spelling is accidentally right, and the right answer
 *                                      arrived by a route that is wrong for every other field.
 *   `signedQty = side === "SHORT" ? -Math.abs(...)` — already normalised, so the Day dollars are the
 *                                      same either way.
 *
 * One expression on this screen changes its answer when the wire changes spelling and the other does
 * not. That is the disagreement, and it is visible without deciding which number is right.
 */
describe("one position, two spellings, one detail screen (#855)", () => {
  const SPELLINGS = [
    { name: "unsigned", quantity: 10 },
    { name: "signed", quantity: -10 },
  ];

  it("the two spellings really are the same position", () => {
    // Vacuity guard for the pair: same side, same size, different spelling. Without this the
    // equality below could hold because both fixtures were identical.
    expect(SPELLINGS.map((s) => Math.abs(s.quantity))).toEqual([10, 10]);
    expect(SPELLINGS[0].quantity).not.toBe(SPELLINGS[1].quantity);
  });

  it("market value does not change when the wire changes spelling", () => {
    // MEASURED: unsigned renders `$1,100`, signed renders `$-1,100`. The signed spelling's SIGN is
    // accidentally right — `110 * -10` — and only its glyph differs from the rest of the screen.
    // Read as numbers so the disagreement that fails here is the one that matters.
    const [unsigned, signed] = SPELLINGS.map((s) =>
      marketValue(renderText(position({ side: "SHORT", quantity: s.quantity }), 110, 100)),
    );
    // The oracle first — a short of 10 marked at 110 is a $1,100 liability — so "they agree" cannot
    // be satisfied by both reporting +1100.
    expect(unsigned).toBe(-1100);
    expect(signed).toBe(-1100);
    expect(unsigned).toBe(signed);
  });

  it("the unrealized headline does not contradict itself in either spelling", () => {
    // MEASURED on the signed spelling: `Unrealized P&L  +$100  -10.00%`. The dollars and the percent
    // point opposite ways on the largest number on the screen. This is `computePnl`'s double
    // negation arriving at the surface an operator actually reads.
    for (const s of SPELLINGS) {
      const text = renderText(position({ side: "SHORT", quantity: s.quantity }), 110, 100);
      const m = text.match(/Unrealized P&L\|([+−]\$[\d,]+)\|(-?[\d.]+)%/);
      expect(m, `no unrealized headline rendered for the ${s.name} spelling`).toBeTruthy();
      expect(m![1], `${s.name}: a short marked above its entry is DOWN`).toBe("−$100");
      expect(m![2]).toBe("-10.00");
    }
  });

  it("the quantity is displayed as a share count everywhere on the screen", () => {
    // TWO ANSWERS TO ONE QUESTION, closed. `PortfolioTile.short.test.ts` already pins that its
    // quantity column reads `10` for both spellings; this screen renders the same fact twice more
    // and neither was covered — the header at `:265` (`-10 @ 100.00`) and the Quantity row at `:285`
    // (`Quantity -10`). A rule enforced on one surface and not its sibling is not a rule.
    for (const s of SPELLINGS) {
      const text = renderText(position({ side: "SHORT", quantity: s.quantity }), 110, 100);
      expect(text, `${s.name}: the identity header`).toContain("|10 @ 100.00|");
      expect(text, `${s.name}: the identity header`).not.toContain("|-10 @ ");
      expect(text, `${s.name}: the Quantity row`).toContain("|Quantity|10|");
      expect(text, `${s.name}: the Quantity row`).not.toContain("|Quantity|-10|");
    }
  });

  it("the FLATTEN confirmation never offers to buy back a negative number of shares", () => {
    // MEASURED on the signed spelling: "Slide to FLATTEN AAPL — buy back -10 shares", and below it
    // "Buys back -10." This is the destructive-action confirmation, and it is the one place on the
    // screen where a nonsense quantity is not merely a wrong reading — it is what the operator is
    // being asked to approve. A share count is a magnitude; the direction is already in the verb.
    for (const s of SPELLINGS) {
      const text = renderText(position({ side: "SHORT", quantity: s.quantity }), 110, 100);
      expect(text, `${s.name} spelling`).toContain("buy back 10 shares");
      expect(text, `${s.name} spelling`).not.toContain("-10 shares");
    }
  });

  it("the day cell already does not change with the spelling", () => {
    // The sibling that passes: `signedQty` on line 248 normalises with `Math.abs`, so this half of
    // the screen is spelling-proof today. It is the shape the market-value line has to reach.
    const [unsigned, signed] = SPELLINGS.map((s) =>
      daySection(renderText(position({ side: "SHORT", quantity: s.quantity }), 110, 100)),
    );
    expect(unsigned).toContain("−$100");
    expect(signed).toContain("−$100");
  });
});
