/**
 * Portfolio row grouping (#273).
 *
 * Pure, so it can be tested without a DOM — the same split `books.ts` uses. It lived inside
 * `ManagedPortfolioTile.tsx` until the rows had to be regrouped, at which point there was no way to
 * assert the grouping without rendering a tile.
 */
import type { TradeDTO } from "@/lib/api/types";
import { money } from "./books";
import { signedQty } from "@/lib/framework/signedQty";

export interface Group {
  instrumentId: string;
  /** The ONE strategy this row belongs to. A row is a position, not a symbol (#273). */
  strategyId: string;
  symbol: string;
  cycles: TradeDTO[];
  netQty: number; // signed native net across this node's cycles (LONG +, SHORT -)
  held: number;
  armed: number;
  realized: number; // Σ cycle realized
  avg: number | null; // held cycle avg cost — QUANTITY-WEIGHTED across cycles, not last-writer-wins
  avgQty?: number; // running Σ|qty| behind `avg`
  avgCost?: number; // running Σ(px × |qty|) behind `avg`
  last: number | null; // mark price
  unrealized: number | null; // Σ cycle unrealized (null = no mark)
  priorClose?: number | null; // prior session close, for today's move — from the shared today_ranges plane
}

/**
 * One row per POSITION — instrument AND strategy — not per symbol (#273).
 *
 * Grouping by symbol alone merged AEM's MANUAL-001 and MOMENTUM-002 legs into a single netted row of 56
 * with both strategies listed underneath as a footnote. That is wrong on every axis this system cares
 * about: strategies are the unit of account and each has its own P&L; Nautilus keeps them genuinely
 * separate (NETTING position ids are `{instrument}-{strategy_id}`); managers are armed per position, so
 * one row cannot say "this leg is protected and that one is not"; and the legs diverge, since MOMENTUM
 * rotates on its own schedule and the manual leg does not.
 *
 * It also broke the row's own click target. `heldPositionKey` needs a single `strategy_id:instrument_id`,
 * so a merged row had none to offer and fell back to the symbol/chart surface — meaning the two
 * multi-strategy positions could not be opened or managed from this tab at all.
 */
export function groupByPosition(trades: TradeDTO[]): Group[] {
  const by = new Map<string, Group>();
  for (const t of trades) {
    // JSON, not a separator string. Both ids are typed as unconstrained `string` in the generated
    // schema, so ANY separator can in principle appear inside one and let `A|B`/`C` collide with
    // `A`/`B|C`. JSON encodes the boundary rather than assuming a character is unused.
    // (codex review, Low.)
    const key = JSON.stringify([t.instrument_id, t.strategy_id]);
    let g = by.get(key);
    if (!g) {
      g = {
        instrumentId: t.instrument_id,
        strategyId: t.strategy_id,
        symbol: t.instrument_id.split(".")[0],
        cycles: [],
        netQty: 0,
        held: 0,
        armed: 0,
        realized: 0,
        avg: null,
        avgQty: 0,
        avgCost: 0,
        last: null,
        unrealized: null,
      };
      by.set(key, g);
    }
    g.cycles.push(t);
    g.realized += money(t.realized_pnl);
    // last_px / unrealized_pl are marked server-side (generated type may lag — read defensively).
    const lp = (t as { last_px?: number | null }).last_px;
    const up = (t as { unrealized_pl?: number | null }).unrealized_pl;
    if (t.is_capital_deployed) {
      g.held += 1;
      // THE PREDICATE, not the rule written out again (#855). This line applied the side to a quantity
      // it had not normalised, while the two lines below already took `Math.abs` for the weighted
      // average — so the one read that decided the row's DIRECTION was the only one in the function
      // that trusted the sign it was handed. Handed a signed short it negated twice and rendered the
      // row LONG; mixed with an unsigned leg it netted to 0 and the cell read the word "flat" while
      // twenty short shares were held.
      g.netQty += signedQty(t.side, t.quantity);
      // WEIGHTED, not last-writer-wins. Two strategies holding the same instrument — 100 @ 100 and
      // 50 @ 130 — showed net 150 at Avg $130.00, the second cycle's price, rather than $110.00.
      // The displayed average belonged to whichever cycle happened to be last in the list.
      if (t.avg_px_open != null && t.quantity) {
        g.avgQty = (g.avgQty ?? 0) + Math.abs(t.quantity);
        g.avgCost = (g.avgCost ?? 0) + t.avg_px_open * Math.abs(t.quantity);
        g.avg = g.avgCost / g.avgQty;
      }
      if (lp != null) g.last = lp;
      if (up != null) g.unrealized = (g.unrealized ?? 0) + up;
    }
    if (t.state === "ARMED") g.armed += 1;
  }
  // HELD-anchored positions first, then armed/flat; each alpha by symbol, then by strategy.
  // The strategy tie-break matters now that two rows can share a symbol: without it their order follows
  // trade arrival order and jitters between frames. (codex review, Low.)
  return [...by.values()].sort(
    (a, b) =>
      Number(b.held > 0) - Number(a.held > 0) ||
      a.symbol.localeCompare(b.symbol) ||
      a.strategyId.localeCompare(b.strategyId),
  );
}

// Same shape as UNCLAIMED_COLS so the two position tables read consistently in one tab.


/**
 * The detail-pane key for a row, or null when it holds nothing.
 *
 * A row is now exactly one instrument and one strategy, so the key is unambiguous whenever anything is
 * held — regardless of how many CYCLES sit behind it. `heldPositionKey` cannot be used here: it returns
 * a key only when exactly ONE cycle is held, so a strategy that opened, closed and reopened the same
 * name would still fall through to the chart. (codex review, High.)
 */
export function positionKeyFor(group: Group): string | null {
  return group.held > 0 ? `${group.strategyId}:${group.instrumentId}` : null;
}
