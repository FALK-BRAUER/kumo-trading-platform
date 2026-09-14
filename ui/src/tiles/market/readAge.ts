/**
 * How old the rotation read is, said where the read is (#351 follow-up).
 *
 * WHY THIS EXISTS. On 2026-08-20 at 06:41 ET the Market tile was serving a payload generated
 * 2026-08-19 14:05 ET — mid-session the previous day, 16.7 hours old — and nothing on screen said so.
 * The operator asked "premarket or yesterday?" and the answer was neither. The only clue was
 * `read generated 8/19/2026, 2:05:48 PM` rendered as a raw locale string BELOW all 25 rows, leaving the
 * reader to scroll past everything and do the subtraction.
 *
 * THE TILE'S EXISTING STALENESS FLAG MEASURES THE WRONG THING. `· source stale` keys on the react-query
 * fetch status: the HTTP request is perfectly fresh, it is the CONTENT that is a day old, so that flag
 * can never fire for this failure. A staleness signal that cannot detect the staleness in front of it is
 * worse than none — it reads as an all-clear.
 *
 * AND IT IS THE DETECTOR FOR A DEAD REFRESH. `scripts/refresh_rotation.py` shells out to fintrack on the
 * HOST, so it cannot run inside the stack; whatever schedules it lives outside and can stop silently —
 * which is exactly what happened, the script having been run once by hand and never again. Age on screen
 * is what makes that visible without anyone going to look.
 */

export type AgeTone = "fresh" | "aging" | "stale";

export interface ReadAge {
  hours: number;
  tone: AgeTone;
  /** Short, human, and never a bare timestamp — the reader must not have to do the subtraction. */
  label: string;
}

/** Aging is a courtesy; STALE is decided by sessions, not by an hour count — see below. */
const AGING_H = 6;

/**
 * Epoch ms of the most recent ET session close (16:00) at or before `nowMs`.
 *
 * Weekend-aware, holiday-unaware — the same limitation `marketSession` carries, and for the same reason:
 * a second, cleverer clock that disagrees with the first is worse than one honest simplification.
 */
function lastSessionClose(nowMs: number): number {
  for (let back = 0; back < 7; back++) {
    const d = new Date(nowMs - back * 86_400_000);
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      weekday: "short",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(d);
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
    if (get("weekday") === "Sat" || get("weekday") === "Sun") continue;
    // 16:00 ET on that calendar date. Built from the ET date so DST is the zone's problem, not ours.
    const close = Date.parse(`${get("year")}-${get("month")}-${get("day")}T16:00:00${etOffset(d)}`);
    if (Number.isFinite(close) && close <= nowMs) return close;
  }
  return nowMs;
}

/** -04:00 or -05:00, whichever this instant is in. */
function etOffset(d: Date): string {
  const name = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", timeZoneName: "shortOffset" })
    .formatToParts(d).find((p) => p.type === "timeZoneName")?.value ?? "GMT-4";
  const m = name.match(/GMT([+-]\d{1,2})/);
  const h = m ? parseInt(m[1], 10) : -4;
  return `${h < 0 ? "-" : "+"}${String(Math.abs(h)).padStart(2, "0")}:00`;
}

export function readAge(generated: string | null | undefined, nowMs: number): ReadAge | null {
  if (!generated) return null;
  const t = Date.parse(generated);
  if (!Number.isFinite(t)) return null;
  const hours = (nowMs - t) / 3_600_000;
  // A payload from the FUTURE is a clock problem, not a fresh read, and must not render as healthy.
  if (hours < -0.5) return { hours, tone: "stale", label: "generated in the future — check the clock" };
  // STALE MEANS "A SESSION HAS CLOSED SINCE THIS WAS COMPUTED", not "N hours have passed". The read that
  // prompted this was 16.7h old — inside any 24h threshold — yet it was generated at 14:05 ET, two hours
  // BEFORE that day's close, so it never saw the session it claims to describe. An hour count cannot
  // express that; the session boundary is the fact that matters for a daily read.
  const tone: AgeTone = t < lastSessionClose(nowMs) ? "stale" : hours >= AGING_H ? "aging" : "fresh";
  return { hours, tone, label: `${humanAge(hours)} old` };
}

function humanAge(hours: number): string {
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m`;
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}
