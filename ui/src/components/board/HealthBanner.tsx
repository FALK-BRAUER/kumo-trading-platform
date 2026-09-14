"use client";

/**
 * HealthBanner (#26) — the single global connection/health strip. Reads the one app-level truth
 * (`useHealth`) and shows a coloured banner when anything is wrong: API unreachable, engine offline, feed
 * stale, or the live stream dropped. Silent when healthy. A trader must never mistake a dead backend for a
 * flat account — this is that guarantee, in one place.
 */
import { useEffect, useState } from "react";
import { useHealth, type HealthLevel } from "@/lib/framework/health";

/** EXPORTED so `healthLevels.test.ts` can drive itself from this map rather than a hand-written list.
 *  Adding a level here with no case there goes red — which is the point: the next level added is the
 *  one a maintained list would miss, and that is how seven levels came to be unpinned. */
export const STYLE: Record<Exclude<HealthLevel, "ok">, string> = {
  apiDown: "bg-red-950/80 text-red-200 border-red-800",
  engineDown: "bg-red-950/80 text-red-200 border-red-800",
  reconcileDrift: "bg-red-950/80 text-red-200 border-red-800", // held-but-invisible positions = critical
  // Same red: a mis-stated held size is size the operator can act on and does not own. The book
  // nets correctly against the broker, so nothing else on the screen will contradict it.
  ownershipViolation: "bg-red-950/80 text-red-200 border-red-800",
  feedStale: "bg-amber-950/70 text-amber-200 border-amber-800/70",
  degraded: "bg-amber-950/70 text-amber-200 border-amber-800/70",
  wsIssues: "bg-amber-950/70 text-amber-200 border-amber-800/70",
};

export function HealthBanner() {
  // A ticking clock so tick-age staleness re-evaluates without waiting on the next poll.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, []);

  const health = useHealth(now);
  if (health.level === "ok") return null;

  return (
    <div
      role="status"
      className={`border-b px-4 py-1.5 text-center font-mono text-[11px] font-semibold ${STYLE[health.level]}`}
    >
      ● {health.message}
    </div>
  );
}
