/**
 * The 1D equity curve, derived rather than fetched (#345 item 4).
 *
 * `exec_client.py:91` fetches `1W/1H`, `1M/1D`, `3M/1D`, `all/1D` — **there is no 1D series**. So the
 * DEFAULT tab rendered "not enough history yet" directly beneath a header quoting that day's move. The
 * chart was structurally empty, not empty because the account was young.
 *
 * The points already exist. The 1W series is hourly, so today's session is a suffix of it, and slicing
 * that costs nothing where a fifth broker call would cost a request and another thing to keep in sync.
 *
 * WHY THE SESSION AND NOT "THE LAST 24 HOURS". A trading day is not a rolling window: at 09:45 ET the
 * honest answer is 15 minutes of curve, not yesterday afternoon dragged in behind it. Anchoring on the
 * ET calendar date also makes the chart agree with `last_equity`, which is the broker's own prior-session
 * close — the figure the header's delta is measured against.
 */
import type { EquityPoint } from "@/components/ds/EquityCurve";

/** ET calendar date (YYYY-MM-DD) for an epoch-ms instant. */
function etDate(ms: number): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(ms));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

/**
 * Points from the current ET session only. `t` is epoch SECONDS, as Alpaca sends it.
 *
 * Returns `[]` rather than a fallback when nothing matches — an empty session is a real answer before
 * the open, and inventing yesterday's tail to fill the frame is the failure this repo keeps naming.
 */
export function sessionPoints(points: EquityPoint[], nowMs: number): EquityPoint[] {
  const today = etDate(nowMs);
  return points.filter((p) => Number.isFinite(p.t) && etDate(p.t * 1000) === today);
}

export interface DerivedCurve {
  points: EquityPoint[];
  base_value: number;
  pnl: number;
  timeframe: string;
}

/**
 * A 1D curve built from the 1W series, or null when it cannot be built honestly.
 *
 * `base_value` is the session's FIRST point, so the derived curve's own P&L is measured from where the
 * day started rather than from where the week did. Null when the session has no points — the caller must
 * keep saying "not enough history yet" in that case rather than draw a flat line at the current level,
 * which would assert the day was unchanged.
 */
export function deriveSessionCurve(week: DerivedCurve | undefined, nowMs: number): DerivedCurve | null {
  const pts = sessionPoints(week?.points ?? [], nowMs);
  if (pts.length < 2) return null; // one point is not a curve; it is a dot
  const base = pts[0].equity;
  return {
    points: pts,
    base_value: base,
    pnl: pts[pts.length - 1].equity - base,
    timeframe: "1H",
  };
}
