/**
 * What a printed price actually IS, and whether a percentage may be computed from it (#355, #356).
 *
 * THE INCIDENT. 2026-08-19 08:15 ET, AMGN rendered `431.00 +5.72 +1.34%`. There were no trades: every
 * premarket 5-minute bar had volume 0, the bid was 1 lot at 427.00 against 570 lots at 431.00, and
 * Today's Range was 0.00 – 0.00. The +1.34% was an OFFER measured against yesterday's close.
 *
 * That is not cosmetic. The entry rules key off premarket percentage — `strategy/day-type-playbook.md`:
 * "Pre-market up >1%? → Gap-up → LIMIT 1-3% below pre-mkt" — so the fiction points at lifting a 570-lot
 * ask with one lot on the bid, which is the precise gap-up chase the rule exists to prevent. The tape
 * said the opposite: AMGN closed -0.39% in the final 15 minutes the day before.
 *
 * THREE RULES, from the issue, enforced here rather than in each tile:
 *
 *   R1  Never compute a percentage change from a quote. Only a trade against a prior close. No trade in
 *       the session means render NOTHING — not zero. `+0.00` reads as "did not move"; the truth is
 *       "did not trade", and VCTR showed exactly that at 08:49 while carrying yesterday's close.
 *   R2  Every price states its provenance in TEXT, at every breakpoint. Colour cannot carry it
 *       (globals.css design-system note).
 *   R3  A quote-only price is a RANGE, not a number: bid–ask with both sizes and the spread.
 *
 * HAS IT TRADED TODAY? `todayRange` is the discriminator, and it is the same field the issue used as
 * evidence — Alpaca's daily bar for the session. Absent, or a degenerate 0/0, means no trade has printed.
 * The engine already validates that bar's own ET date before publishing it and tombstones a stale one, so
 * a value here is today's by construction rather than by hope.
 */
import type { MarketSession } from "./market";

export interface Quote {
  bid?: number | null;
  ask?: number | null;
  bid_size?: number | null;
  ask_size?: number | null;
  ts_event?: number | null;
}

export interface TodayRange {
  high?: number | null;
  low?: number | null;
  prevClose?: number | null;
}

export type PriceKind = "traded" | "quote" | "closed" | "unknown" | "unpriced";

export interface PriceState {
  kind: PriceKind;
  /** Plain-words provenance — R2. Shipped as text, never implied by colour alone. */
  label: string;
  /** The single number to print, when there is an honest one. */
  price: number | null;
  /** Percent change vs the prior close. NON-NULL ONLY for `traded` — R1. */
  pct: number | null;
  /** R3: populated only for `quote`. */
  bid: number | null;
  ask: number | null;
  bidSize: number | null;
  askSize: number | null;
  spreadPct: number | null;
}

const num = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

/**
 * WAS THE RANGE DELIVERED AT ALL? Three states, not two — absent is not the same as empty.
 *
 * The `today_ranges` plane is currently empty for every held symbol (#345 item 6), and the first version
 * of this module treated that exactly like a degenerate 0/0 range: "nothing traded". Live on 2026-08-20
 * at 07:01 ET that put "Prior close" under BDX at 187.87 and WHD at 72.99 while the marks were visibly
 * moving and account equity had shifted $135 — asserting a name had not traded when it demonstrably had.
 *
 * That is the same false-certainty this module exists to prevent, produced by the module itself. A
 * missing input means WE DO NOT KNOW, and the honest rendering of that is no claim at all — the rule
 * `broker_protected` and `basis_contested` already follow.
 */
export function rangeKnown(range: TodayRange | null | undefined): boolean {
  return num(range?.high) !== null && num(range?.low) !== null;
}

/** Did anything print in today's session? A degenerate 0/0 range is Alpaca's "nothing traded". */
export function hasTradedToday(range: TodayRange | null | undefined): boolean {
  if (!rangeKnown(range)) return false;
  return (num(range?.high) as number) > 0 && (num(range?.low) as number) > 0;
}

const EMPTY = { bid: null, ask: null, bidSize: null, askSize: null, spreadPct: null } as const;

export function priceState(input: {
  price?: number | null;
  session: MarketSession;
  todayRange?: TodayRange | null;
  quote?: Quote | null;
}): PriceState {
  const price = num(input.price);
  const prevClose = num(input.todayRange?.prevClose);
  const traded = hasTradedToday(input.todayRange);

  // A TRADE IS THE ONLY THING THAT EARNS A PERCENTAGE. Note this does not require the session to be
  // OPEN: a premarket print is a real trade against yesterday's close, and refusing to quote it would be
  // the opposite error. What is refused is a percentage with no trade behind it.
  if (traded && price !== null) {
    const pct = prevClose !== null && prevClose !== 0 ? ((price - prevClose) / prevClose) * 100 : null;
    return { kind: "traded", label: sessionLabel(input.session), price, pct, ...EMPTY };
  }

  // NOTHING TRADED. In a session where quoting happens, show the market as what it is — two sides and a
  // spread — rather than picking one side and calling it the price. That single choice is the whole bug:
  // the ask, printed alone, became a +1.34% move.
  const bid = num(input.quote?.bid);
  const ask = num(input.quote?.ask);
  if ((input.session === "PRE" || input.session === "OPEN" || input.session === "AFTER") && bid !== null && ask !== null) {
    const mid = (bid + ask) / 2;
    return {
      kind: "quote",
      label: "Quote only",
      price: null,
      pct: null, // R1, unconditionally
      bid,
      ask,
      bidSize: num(input.quote?.bid_size),
      askSize: num(input.quote?.ask_size),
      spreadPct: mid > 0 ? ((ask - bid) / mid) * 100 : null,
    };
  }

  // NO RANGE WAS DELIVERED — so we do not know whether it traded, and must not say. Rendering the price
  // bare is the honest answer: it is the number we hold, with no claim attached to it.
  if (price !== null && !rangeKnown(input.todayRange)) {
    return { kind: "unknown", label: "", price, pct: null, ...EMPTY };
  }

  // The range WAS delivered and says nothing printed. The last number we hold is a CLOSE, and it must
  // say so — carried forward and labelled, never dressed as a live price with a 0.00% move.
  if (price !== null) {
    return { kind: "closed", label: "Prior close", price, pct: null, ...EMPTY };
  }
  return { kind: "unpriced", label: "No price", price: null, pct: null, ...EMPTY };
}

/** Which session the trade came from, in words. */
function sessionLabel(session: MarketSession): string {
  switch (session) {
    case "PRE":
      return "Pre-market";
    case "AFTER":
      return "After hours";
    case "OPEN":
      return "Traded";
    default:
      return "Last close";
  }
}
