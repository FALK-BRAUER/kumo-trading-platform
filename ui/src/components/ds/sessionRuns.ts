/**
 * Split an intraday equity series into contiguous runs by MARKET SESSION, so extended hours can be
 * drawn differently from the regular session.
 *
 * WHY IT IS WORTH DRAWING. #536 gave the 1D chart a real 1Min series WITH extended hours, and the
 * shape immediately changed: a smooth low-amplitude stretch, then a sharp discontinuity, then dense
 * volatility. That discontinuity IS the 09:30 open — thin pre-market marks giving way to a fully
 * quoted book. Undifferentiated, it reads as a crash at an arbitrary point in the day.
 *
 * Pre- and post-market equity is also a WEAKER NUMBER than the regular session's, and not only
 * cosmetically: marks come from thin quotes, so the same position is priced with far less
 * information. Rendering both in one colour asserts they are the same kind of measurement.
 *
 * ET, NOT UTC AND NOT LOCAL. Session boundaries are exchange-local and move with US daylight saving,
 * so they are derived from the America/New_York wall clock rather than a fixed UTC offset. A UTC
 * constant would be right for half the year and silently wrong for the other half.
 */

export type Session = "pre" | "regular" | "post" | "closed";

export interface SessionRun<P> {
  session: Session;
  points: P[];
}

/**
 * Minutes since midnight from an already-extracted hour/minute pair.
 *
 * SPLIT OUT SO THE `% 24` CAN BE TESTED AT ALL. `hour12: false` reports midnight as "24" in some
 * engines and "00" in others; this runtime returns "00", so a test that formats a real midnight
 * instant passes whether or not the guard exists — measured, by deleting the `% 24` and watching
 * nothing go red. The guard is real (a 24 would put 00:05 at 1445 minutes, after the close instead of
 * before the pre-open) and it is only falsifiable if the input can be supplied directly.
 */
export function minutesFrom(hour: number, minute: number): number {
  return (hour % 24) * 60 + minute;
}

/** Minutes since ET midnight for an epoch-SECONDS instant. */
export function etMinutes(tSec: number): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(new Date(tSec * 1000));
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? "0");
  return minutesFrom(get("hour"), get("minute"));
}

const PRE_OPEN = 4 * 60; //  04:00 ET — Alpaca's extended session starts here
const OPEN = 9 * 60 + 30; //  09:30 ET
const CLOSE = 16 * 60; //     16:00 ET
const POST_CLOSE = 20 * 60; // 20:00 ET

export function sessionOf(tSec: number): Session {
  const m = etMinutes(tSec);
  if (m >= OPEN && m < CLOSE) return "regular";
  if (m >= PRE_OPEN && m < OPEN) return "pre";
  if (m >= CLOSE && m < POST_CLOSE) return "post";
  return "closed";
}

/**
 * Contiguous runs, each tagged with its session.
 *
 * RUNS OVERLAP BY ONE POINT, DELIBERATELY. Each run after the first repeats the previous run's last
 * point, so consecutive paths share an endpoint and the line has no visual gap at the boundary. Drawn
 * without it, the open shows as a break in the curve — which is exactly the artefact this is meant to
 * explain rather than introduce.
 */
export function sessionRuns<P extends { t: number }>(points: P[]): SessionRun<P>[] {
  const runs: SessionRun<P>[] = [];
  for (const p of points) {
    const s = sessionOf(p.t);
    const last = runs[runs.length - 1];
    if (last && last.session === s) {
      last.points.push(p);
      continue;
    }
    // Repeat the boundary point so the segments join.
    runs.push({ session: s, points: last ? [last.points[last.points.length - 1], p] : [p] });
  }
  return runs;
}
