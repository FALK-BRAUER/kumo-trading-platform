/**
 * What a flat-but-ARMED row is actually waiting for (#402).
 *
 * 2026-08-21, looking at CRAK:
 *
 *   > Crak is interesting also. Is it because we have a breakout ordered? maybe it would be good to
 *   > actually keep also the peak and stop and rebuy items with 0 in the manual portfolio
 *
 * His reading was right. CRAK held nothing and had a resting bracket entry that had not triggered, so
 * the row rendered `flat · NO PRICE · —` with a sub-line reading `0 held · 1 armed`.
 *
 * "1 armed" is a COUNT WITH NO NOUN. From it, three completely different situations are
 * indistinguishable, and two of them mean money is committed at the venue:
 *
 *   - a resting entry that has not triggered      (CRAK, today)
 *   - a stop-and-reenter waiting for a reclaim
 *   - a PEAK manager still attached to a position that has gone flat
 *
 * WHAT THIS CAN AND CANNOT ANSWER. The trades plane carries `working_orders` per cycle, so a resting
 * ENTRY can be named exactly — including its side, type and trigger — with no new data binding. It does
 * NOT carry the manager's kind: `manager_id` is null on CRAK's cycle even though a manager row exists.
 * Naming the mechanism for the other two cases needs the `/managers` plane bound to this tile, which is
 * a separate change; until then this returns a bare `armed` rather than inventing a noun.
 *
 * HOW THE ENTRY IS TOLD FROM THE EXIT LEGS. Not by counting — a bracket can rest as entry + stop with
 * one order per side, which is a tie. By the invariant instead: **a protective stop always rests on the
 * REDUCING side**. So the stop's side is the exit side, and the entry is the opposite one. CRAK, live:
 *
 *     SELL LIMIT       180 @ 57.15    <- target leg
 *     BUY  LIMIT       180 @ 57.09    <- the entry
 *     SELL STOP_MARKET 180 trig 57.06 <- protective stop => reducing side is SELL
 */
import type { WorkingOrderDTO } from "@/lib/api/types";

export type ArmedReading =
  | { kind: "entry"; side: "BUY" | "SELL"; orderType: string; price: number | null; qty: number }
  | { kind: "armed" };

const _STOP_TYPES = new Set(["STOP_MARKET", "STOP_LIMIT", "TRAILING_STOP_MARKET", "TRAILING_STOP_LIMIT"]);
/** Order statuses that are actually resting at the venue. Mirrors `protection._OPEN_STATUSES`. */
const _RESTING = new Set([
  "ACCEPTED", "HELD", "NEW", "PARTIALLY_FILLED", "PENDING_NEW", "SUBMITTED",
  "PENDING_UPDATE", "PENDING_CANCEL", "PENDING_REPLACE", "TRIGGERED", "ACCEPTED_FOR_BIDDING",
]);

function resting(orders: WorkingOrderDTO[] | undefined | null): WorkingOrderDTO[] {
  return (orders ?? []).filter((o) => _RESTING.has(String(o.status ?? "").toUpperCase()));
}

/**
 * The one thing a flat armed row is waiting for, or a bare `armed` when it cannot be named.
 *
 * `null` when nothing is armed at all — the caller renders nothing, which is right: a row with no
 * commitment needs no explanation.
 */
export function armedReading(orders: WorkingOrderDTO[] | undefined | null): ArmedReading | null {
  const live = resting(orders);
  if (live.length === 0) return null;

  // 1. A LONE resting order on a flat cycle has nothing to reduce, so it opens — whatever its type. A
  //    stop-entry breakout rests as a single STOP_MARKET and must not be mistaken for a protective one.
  let entry: WorkingOrderDTO | undefined;
  if (live.length === 1) {
    entry = live[0];
  } else {
    // 2. THE MINORITY SIDE. A bracket's exit legs share a side (target + stop) and the entry is alone
    //    opposite them — CRAK rests 1 BUY against 2 SELLs. This is the common shape and needs no
    //    assumption about which leg is protective.
    const buys = live.filter((o) => o.side === "BUY");
    const sells = live.filter((o) => o.side !== "BUY");
    if (buys.length === 1 && sells.length > 1) entry = buys[0];
    else if (sells.length === 1 && buys.length > 1) entry = sells[0];
    else if (buys.length === 1 && sells.length === 1) {
      // 3. A TIE — an entry resting with only its protective stop, no target. Broken by the invariant
      //    that a PROTECTIVE stop rests on the REDUCING side, so the entry is the opposite one.
      const stop = live.find((o) => _STOP_TYPES.has(String(o.order_type ?? "").toUpperCase()));
      if (stop) entry = live.find((o) => o.side !== stop.side);
    }
  }

  // 4. Anything else — two same-side orders with nothing to distinguish them — stays unnamed. Guessing
  //    would put a confident wrong sentence on a row about committed capital, which is worse than the
  //    bare count it replaced.
  if (!entry) return { kind: "armed" };
  return {
    kind: "entry",
    side: entry.side === "SELL" ? "SELL" : "BUY",
    orderType: String(entry.order_type ?? ""),
    price: typeof entry.price === "number" ? entry.price : (entry.trigger_price ?? null),
    qty: typeof entry.quantity === "number" ? entry.quantity : 0,
  };
}

/** Human phrasing for the sub-line. Short enough for a phone row. */
export function armedLabel(reading: ArmedReading): string {
  if (reading.kind === "armed") return "armed";
  const verb = reading.side === "BUY" ? "buy" : "sell";
  const type = reading.orderType.toLowerCase().replace("_market", "").replace("_limit", " limit");
  const px = reading.price != null ? ` ${reading.price.toFixed(2)}` : "";
  return `awaiting entry · ${verb} ${type}${px}`;
}
