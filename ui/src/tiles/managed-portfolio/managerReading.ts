/**
 * Naming the manager on a position row, and telling a live failure from a fossil (#402, #400).
 *
 * `armedReading` can name a resting ENTRY from the cycle's own orders, but not the MECHANISM: the trades
 * plane carries `manager_id: null` even when a manager row exists (verified on CRAK, 2026-08-21). So the
 * kind has to come from the `/managers` plane, which this reads.
 *
 * THE STALENESS RULE IS STRUCTURAL, NOT A TIMEOUT. `client.ts` already states it on the type: "a stale
 * manager from a closed cycle must not mask a newer one on the position's current cycle — match on this
 * before falling back to instrument/strategy alone." That is exactly the trap this codebase walked into
 * on 2026-08-21: a `peak_watch` that FAILED on 2026-08-11 — against a cycle that closed nine days ago —
 * was read as a live outage and reported as "PEAK is dead", while the failure that actually mattered
 * (A.XNYS, that afternoon, #401) went unnoticed in the same undated list.
 *
 * Matching on `cycle_id` fixes that at the root: a manager attached to a cycle that is no longer the
 * position's current one is HISTORY, whatever its state and whatever its age. An age threshold would
 * have been a guess about how long is too long; the cycle boundary is a fact.
 *
 * Age is still carried, because "failed 3 minutes ago" and "failed at 09:31" are what an operator needs
 * once they know the row is live. It is never the thing that decides relevance.
 */
import type { Manager } from "@/lib/api/client";

/** Kind -> what an operator calls it. Unknown kinds fall back to the raw kind rather than "manager": a
 *  name we do not recognise is still more informative than a category. */
const MECHANISM: Record<string, string> = {
  peak_watch: "peak",
  stop_reenter_watch: "stop & reenter",
  stop_reenter_rearm: "stop & reenter",
  pyramid_watch: "pyramid",
  deferred_flatten: "deferred flatten",
};

export function mechanismLabel(kind: string): string {
  return MECHANISM[kind] ?? kind;
}

/** States that still have something to do. Everything else is over. */
const LIVE_STATES = new Set(["ARMED", "PROPOSED", "APPROVED", "APPLYING"]);

export interface ManagerReading {
  /** Managers still working on THIS cycle. */
  live: Manager[];
  /** Managers that FAILED on THIS cycle — a real alarm, not a fossil. */
  failed: Manager[];
}

/**
 * The managers that belong to one position's CURRENT cycle.
 *
 * `cycleId` null (a pre-#68 row, or a flat cycle with no id) falls back to instrument+strategy, which is
 * the documented degradation — but then a closed cycle's manager can reappear, so callers should prefer
 * a real cycle id wherever they have one.
 */
export function managersFor(
  managers: Manager[] | undefined | null,
  instrumentId: string,
  strategyId: string,
  cycleId: string | null,
): ManagerReading {
  const mine = (managers ?? []).filter(
    (m) => m.instrument_id === instrumentId && m.strategy_id === strategyId,
  );
  // CYCLE FIRST. Only fall back when this position has no cycle id to match on at all — never when it
  // has one and nothing matches, because "nothing on this cycle" is a real answer and replacing it with
  // an older cycle's rows is precisely the #400 misreading.
  const scoped = cycleId != null ? mine.filter((m) => m.cycle_id === cycleId) : mine;
  return {
    live: scoped.filter((m) => LIVE_STATES.has(m.state)),
    failed: scoped.filter((m) => m.state === "FAILED"),
  };
}

/** "3m" / "4h" / "9d" — coarse on purpose; the exact stamp belongs in the tooltip. */
export function ageLabel(iso: string | null | undefined, nowMs: number): string | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return null;
  const mins = Math.floor((nowMs - then) / 60_000);
  if (mins < 0) return null; // a clock skew is not an age
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

/** The sub-line fragment for a failed manager on the current cycle, with its age when known. */
export function failureLabel(m: Manager, nowMs: number): string {
  const age = ageLabel(m.updated_at ?? m.created_at, nowMs);
  return age ? `${mechanismLabel(m.kind)} FAILED ${age} ago` : `${mechanismLabel(m.kind)} FAILED`;
}
