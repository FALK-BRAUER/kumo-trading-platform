/**
 * Reduce the `session` frame to what an operator needs in ten seconds (#212).
 *
 * `research/homescreen.md` argues the page answers one question — "can I leave this alone, or do I
 * need to act?" — and that on a normal day it should be boring. This is that reduction, kept pure so
 * the rules are testable without a DOM.
 *
 * The frame is the strategy's own account of itself. Two things it says are easy to conflate and are
 * kept apart here deliberately:
 *
 *   INTENT   `decision.summary` — "hold 8 · enter 8 · exit 2", what the session set out to do.
 *   OUTCOME  `submitted_count` — orders actually submitted, counted from structured journal rows.
 *
 * On 2026-08-10 those differed: two ranked names hit the position cap, so ten planned actions became
 * eight submitted orders. The daily digest once made the same mistake in the other direction,
 * reporting "entered 6" for a day whose true answer was zero. A tile that shows only the summary
 * repeats it, so the gap is surfaced rather than hidden.
 */
import type { SessionFrame } from "@/lib/framework/datasource/protocol";

/** Risk wording that describes a DEGRADED SYSTEM rather than normal policy.
 *
 * "N account positions are not this strategy's" is deliberately ABSENT. It fires every session
 * because the operator holds FIG manually — a permanent, expected condition — and an alert that is always on
 * teaches the operator to skip the list, which costs more than never having built it. The case where
 * it would matter (an unclaimed position that should not exist, like the HSBC phantom) surfaces as
 * `reconcile_drift` on /health, which is already wired to Telegram. Same event, a channel that is
 * normally silent. */
const DEGRADED = [
  "missing", "not subscribed", "stale", "coverage", "unclaimed",
  "refusing", "halted", "breach", "failed", "unavailable", "disconnected",
  "timeout", "timed out", "rate limit", "denied", "no quote", "no bars", "disabled",
];

/** Wording that CONTAINS a degraded keyword while reporting the opposite. Checked first. */
const BENIGN = ["no breach", "coverage ok", "cleared", "recovered", "resubscribed"];

/** Lifecycle states that are themselves a reason to look, whatever else the frame says. */
const ABNORMAL_STATES = new Set(["HALTED", "LIQUIDATING"]);

/** A frame older than this means the engine has stopped publishing. It republishes every 20s. */
export const STALE_AFTER_MS = 120_000;

export interface StrategySummary {
  /** Lifecycle: TRADING / PAUSED / LIQUIDATING / HALTED, or null before the frame arrives. */
  state: string | null;
  reason: string;
  /** The session the last decision belongs to, and whether that is today. */
  session: string | null;
  decisionIsToday: boolean;
  /** e.g. "hold 8 · enter 8 · exit 2" with the leading state stripped. */
  headline: string;
  submitted: number;
  /** Set only when intent and outcome disagree — planned actions vs orders actually submitted. */
  shortfall: { planned: number; submitted: number } | null;
  /** Errors first, then risk rows describing a degraded system. Verbatim journal wording. */
  attention: string[];
  /** Held names whose peak was never observed, so peak-relative exits are inert for them. */
  unprotected: string[];
  /** True only when nothing needs the operator — see the guard in `summarise`. */
  allClear: boolean;
  /** Milliseconds since the engine built this frame, or null if it carries no timestamp. */
  ageMs: number | null;
  /** The engine has stopped publishing. The frame on screen is frozen, not calm. */
  stale: boolean;
  /** No frame yet — distinct from "nothing happened", which is a very different thing to show. */
  waiting: boolean;
}

export function summarise(frame: SessionFrame | null | undefined,
                          now: number = Date.now()): StrategySummary {
  const empty = !frame || Object.keys(frame).length === 0;
  const lifecycle = (frame as { lifecycle?: { state?: string; reason?: string } } | null)?.lifecycle;
  const decision = frame?.decision ?? null;

  // Deduped: the journal legitimately records the same condition once per attempt, so a single
  // problem arrived three times as three identical rows. Repeating it does not make it more true,
  // and it pushed the rest of the list off the tile.
  const attention = unique([
    ...errorsOf(frame),
    ...riskRows(frame).filter((r) => {
      const t = r.toLowerCase();
      return !BENIGN.some((k) => t.includes(k)) && DEGRADED.some((k) => t.includes(k));
    }),
  ]);

  // A trail whose peak was never observed: give-back and off-peak cannot fire for it (#197 B1).
  // Worth showing, because the position LOOKS protected and is not.
  const unprotected = (frame?.trail ?? [])
    .filter((t) => String(t.quality ?? "").toLowerCase() === "adopted" && usable(t.qty))
    .map((t) => t.symbol);

  const submitted = numberOf(frame, "submitted_count");
  const planned = plannedActions(decision?.summary ?? "");
  // Only a SHORTFALL counts. `submitted > planned` is normal — bracket legs, retries and manager
  // orders all add submissions the decision summary never promised — and reporting it as a
  // discrepancy would cry wolf on ordinary days.
  const shortfall = planned !== null && submitted < planned ? { planned, submitted } : null;

  const ageMs = frameAgeMs(frame, now);
  // The consumer re-pushes the last frame forever, and the WS layer reports no staleness, so a dead
  // engine looks exactly like a quiet one. Age is computed from the engine's OWN timestamp, which
  // stops advancing the moment it dies.
  const stale = ageMs !== null && ageMs > STALE_AFTER_MS;

  return {
    state: lifecycle?.state ?? null,
    reason: lifecycle?.reason ?? "",
    session: frame?.session ?? null,
    decisionIsToday: Boolean(frame?.decision_is_today),
    headline: stripState(decision?.summary ?? ""),
    submitted,
    shortfall,
    attention,
    unprotected,
    // EVERY reason to look must suppress the all-clear. The first version checked only the attention
    // list, so it would print "Nothing needs you." beside a shortfall line, beside a decision that
    // was not today's, and while the strategy sat HALTED. A false all-clear is the worst output this
    // tile can produce: every other failure is a wrong line, that one actively suppresses attention.
    allClear:
      !empty && !stale &&
      attention.length === 0 &&
      unprotected.length === 0 &&
      shortfall === null &&
      !ABNORMAL_STATES.has(String(lifecycle?.state ?? "").toUpperCase()) &&
      (decision === null || Boolean(frame?.decision_is_today)),
    ageMs,
    stale,
    waiting: empty,
  };
}

/**
 * Planned actions from the decision summary — "hold 8 · enter 8 · exit 2" → 10.
 *
 * `hold` is deliberately excluded: holding is the absence of an action, and counting it would make
 * every session look like it fell short. Returns null when the summary cannot be parsed, so an
 * unrecognised format shows no shortfall rather than a wrong one.
 */
export function plannedActions(summary: string): number | null {
  const enter = /enter\s+(\d+)/i.exec(summary);
  const exit = /exit\s+(\d+)/i.exec(summary);
  if (!enter && !exit) return null;
  return Number(enter?.[1] ?? 0) + Number(exit?.[1] ?? 0);
}

/** "TRADING: hold 8 · enter 8 · exit 2" → "hold 8 · enter 8 · exit 2". The state is shown separately. */
function stripState(summary: string): string {
  const i = summary.indexOf(":");
  return (i >= 0 ? summary.slice(i + 1) : summary).trim();
}

function riskRows(frame: SessionFrame | null | undefined): string[] {
  const journal = (frame as { journal?: { kind?: string; summary?: string }[] } | null)?.journal;
  return (journal ?? []).filter((r) => r.kind === "risk").map((r) => String(r.summary ?? ""));
}

function errorsOf(frame: SessionFrame | null | undefined): string[] {
  // The backend carries errors as objects with their own session and timestamp; tolerate strings too
  // so a shape change degrades to showing the text rather than showing nothing.
  const errs = (frame as { errors?: unknown[] } | null)?.errors ?? [];
  return errs.map((e) =>
    typeof e === "string" ? e : String((e as { summary?: string })?.summary ?? "")).filter(Boolean);
}

function numberOf(frame: SessionFrame | null | undefined, key: string): number {
  const v = (frame as Record<string, unknown> | null)?.[key];
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

/** Engine-side build time. `ts` is nanoseconds from the Nautilus clock. */
function frameAgeMs(frame: SessionFrame | null | undefined, now: number): number | null {
  const ts = (frame as { ts?: unknown } | null)?.ts;
  if (typeof ts !== "number" || !Number.isFinite(ts) || ts <= 0) return null;
  return now - ts / 1e6;
}

function unique(rows: string[]): string[] {
  return [...new Set(rows.map((r) => r.trim()))].filter(Boolean);
}

function usable(v: number | null | undefined): boolean {
  return typeof v === "number" && Number.isFinite(v) && v > 0;
}
