/**
 * US market-hours helpers (#113). JSX-free + pure so they unit-test and can guard the order forms.
 *
 * `isUsMarketOpen` checks the US regular session (Mon–Fri 09:30–16:00 ET). It's holiday-UNAWARE (a
 * follow-up can swap in Alpaca's authoritative /v2/clock for holidays / half-days), but it resolves ET via
 * the IANA `America/New_York` zone so EDT⇄EST is handled correctly — a hardcoded offset would be an hour off
 * for ~⅓ of the year, which for a trading-safety guard means blocking legit orders and allowing doomed ones.
 * Extracted from health.ts so the order path and the health banner agree.
 */
/** Which US session a moment falls in. `CLOSED` covers weekends, holidays-as-weekdays and 20:00–04:00. */
export type MarketSession = "PRE" | "OPEN" | "AFTER" | "CLOSED";

/** Minutes past ET midnight, plus the weekday — the one place this file parses a clock. */
function etMinutes(nowMs: number): { weekday: string; mins: number } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(new Date(nowMs));
  const part = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  let hour = parseInt(part("hour"), 10);
  if (hour === 24) hour = 0; // some ICU builds emit "24" for midnight
  return { weekday: part("weekday"), mins: hour * 60 + parseInt(part("minute"), 10) };
}

/**
 * PRE 04:00–09:30 · OPEN 09:30–16:00 · AFTER 16:00–20:00 · CLOSED otherwise (#356).
 *
 * WHY THIS EXISTS AT ALL. A price with no session context is not a price. At 08:49 ET on 2026-08-19 VCTR's
 * "current price" was yesterday's close carried forward — it had not traded that day — and its intraday P&L
 * read exactly +0.00, which the UI rendered as FLAT. Unpriced and flat are very different facts and nothing
 * on screen could tell them apart; the operator asked "is that premarket?" because nothing answered it.
 *
 * Holiday-UNAWARE, exactly as `isUsMarketOpen` always was — a holiday reads as OPEN. Swapping in Alpaca's
 * /v2/clock is the follow-up; what must not happen meanwhile is a SECOND clock derivation appearing
 * elsewhere and disagreeing with this one.
 */
export function marketSession(nowMs: number): MarketSession {
  const { weekday, mins } = etMinutes(nowMs);
  if (weekday === "Sat" || weekday === "Sun") return "CLOSED";
  if (mins >= 9 * 60 + 30 && mins < 16 * 60) return "OPEN";
  if (mins >= 4 * 60 && mins < 9 * 60 + 30) return "PRE";
  if (mins >= 16 * 60 && mins < 20 * 60) return "AFTER";
  return "CLOSED";
}

/**
 * The US regular session. DERIVED FROM `marketSession`, not computed again.
 *
 * Two derivations of one fact disagree — this repo has measured that several times. The order path and the
 * health banner both gate on this, so an open-check that could drift from the session label would let one
 * screen say PRE while another accepted a market order.
 */
export function isUsMarketOpen(nowMs: number): boolean {
  return marketSession(nowMs) === "OPEN";
}

/**
 * True when a MARKET order placed right now would be canceled by the broker: a market order can't rest, so
 * outside regular hours the venue kills it in seconds (Nautilus/Alpaca). Limit and stop orders rest fine, so
 * they are never blocked. `orderType` is the effective entry type ("market" | "limit" | "stop" | …).
 */
export function marketOrderWouldBeCanceled(orderType: string, nowMs: number): boolean {
  return orderType.toLowerCase() === "market" && !isUsMarketOpen(nowMs);
}
