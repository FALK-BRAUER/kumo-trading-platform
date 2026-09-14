/**
 * #855 — a short cannot be moved to a strategy at all, because its bound is negative.
 *
 * FIVE LINES, not one. The AST scan reports only `:62`, because that is the only one where a quantity
 * is an operand of a comparison; the tests below fail on four more that the scan structurally cannot
 * see — an argument, a subtraction guarded behind the comparison, and two pieces of JSX text.
 *
 *     :57   const [qty, setQty] = useState(String(source.quantity));      the value offered
 *     :62   const qtyOk = ... qtyNum > 0 && qtyNum <= source.quantity;    the gate (scan sees this)
 *     :63   const remainder = qtyOk ? source.quantity - qtyNum : 0;       what stays behind
 *     :88   <span>of {source.quantity} →</span>                            the size, rendered
 *     :107  quantity must be between 1 and {source.quantity}               the refusal, rendered
 *
 * `source` is an unclaimed broker row, and the broker signs its quantity — `venue_qty` is `-10` for a
 * short of 10 (`ExternalActivityDTO.venue_qty`, and `protection.py:808` signs `market_value` off the
 * same sign). So the field pre-fills with "-10", which fails `qtyNum > 0`; and every positive
 * quantity the operator could type fails `qtyNum <= -10`. The two clauses close the interval from
 * both ends: there is no number that satisfies them, and the tile renders "quantity must be between 1
 * and -10" with the button disabled, forever.
 *
 * THIS SITE IS NOT IN THE TICKET. It was found by the structural scan in `signedQty.short.test.ts`,
 * which is the argument for the scan: the five reported rows were the five somebody looked at.
 *
 * It also fails differently from the other five. Those render a wrong number — recoverable, once
 * noticed. This one renders a refusal, and a refusal reads as the system working: an unclaimed short
 * simply cannot be adopted, and nothing anywhere says why. It is the same shape as the QC345 sleeve
 * that never placed an order in its life while every surface said TRADING.
 *
 * Driven through `PositionTransferView`, the exported presentational half, rendered with
 * `react-dom/server`. The assertions are all on the FIRST render, because there is no DOM here to
 * type into — which is enough: the defect is present before any interaction, in what the tile offers.
 */
import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { PositionTransferView, type TransferSource } from "./PositionTransferTile";

/** An unclaimed broker row as `/external_activity` emits it. `quantity` carries the broker's sign. */
const source = (over: Partial<TransferSource>): TransferSource => ({
  instrument_id: "AAPL.XNAS",
  strategy_id: "EXTERNAL",
  side: "LONG",
  quantity: 10,
  ...over,
});

function render(s: TransferSource): string {
  const html = renderToStaticMarkup(
    createElement(PositionTransferView as never, {
      source: s,
      targets: ["MOMENTUM-002"],
      pricingMode: "CARRY_OVER",
    } as never),
  );
  return html;
}

const text = (html: string) => html.replace(/<[^>]+>/g, "|").replace(/&amp;/g, "&").replace(/\|+/g, "|");
/** React renders a disabled button as a bare `disabled=""` attribute. */
const buttonDisabled = (html: string) => /<button[^>]*\sdisabled=""/.test(html);
/** The value the quantity field is pre-filled with — what the operator is offered by default. */
const prefilled = (html: string) => html.match(/<input[^>]*value="([^"]*)"/)?.[1] ?? "";

const SIGNED_SHORT = source({ side: "SHORT", quantity: -10 });

describe("the fixture can express the bug", () => {
  it("the row is a short whose broker quantity carries the sign", () => {
    // Vacuity guard. An unsigned fixture is the case that already works, and every assertion below
    // would pass on it while saying nothing about the signed spelling.
    expect(SIGNED_SHORT.side).toBe("SHORT");
    expect(SIGNED_SHORT.quantity).toBeLessThan(0);
    expect(Math.abs(SIGNED_SHORT.quantity)).toBe(10);
  });

  it("the view really does render the quantity gate — it is not off-screen", () => {
    // If the gate were absent from the markup the disabled/bound assertions would be vacuous.
    const html = render(source({}));
    expect(prefilled(html)).toBe("10");
    expect(buttonDisabled(html)).toBe(false);
  });
});

describe("a short can be moved to a strategy (#855)", () => {
  it("the quantity offered by default is the size held, not its negation", () => {
    // "-10" is not a quantity anyone can transfer. The field opens on a value its own validator
    // rejects, which is the first thing the operator sees.
    expect(prefilled(render(SIGNED_SHORT))).toBe("10");
  });

  it("moving the whole 10 shares is permitted", () => {
    // The bound is `qtyNum > 0 && qtyNum <= source.quantity`. With `source.quantity` at -10 the two
    // clauses close the interval from both ends and no quantity exists that satisfies both.
    expect(buttonDisabled(render(SIGNED_SHORT))).toBe(false);
  });

  it("the tile does not tell the operator the valid range is 1 to -10", () => {
    // The rendered refusal, which is the part that makes this invisible: it reads like a validation
    // message rather than like a tile that can never accept anything.
    const t = text(render(SIGNED_SHORT));
    expect(t).not.toContain("between 1 and -10");
  });

  it("the size it offers to move is stated as a magnitude", () => {
    // `of {source.quantity} →` sits beside the input. A short of 10 is a position of 10 shares; the
    // direction is `side`, and it is already displayed separately.
    expect(text(render(SIGNED_SHORT))).toContain("of 10 →");
  });

  it("an unsigned short is unaffected — the defect is the spelling, not the side", () => {
    // The sibling that passes today, so the failures above point at the sign and not at shorts.
    const html = render(source({ side: "SHORT", quantity: 10 }));
    expect(prefilled(html)).toBe("10");
    expect(buttonDisabled(html)).toBe(false);
  });
});
