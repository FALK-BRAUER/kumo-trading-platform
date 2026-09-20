"use client";

/**
 * BusyBanner (#269 follow-up) — what the engine is doing RIGHT NOW, in the strip where problems appear.
 *
 * 2026-08-17, after a flatten that actually worked: "there was no proper feedback on flatten. The
 * stock looked unchanged."
 *
 * The exit had genuinely become slower. It now cancels the resting stop, waits for the venue to confirm,
 * waits for Alpaca to release the reserved shares, and only then closes — his NBIS flatten took 11
 * seconds. The screen said nothing for all of it. A destructive control that looks inert is one an
 * operator presses again, and pressing FLATTEN twice is how a position gets reversed instead of closed.
 *
 * ELAPSED TIME IS SHOWN BECAUSE IT IS WHAT WE ACTUALLY KNOW. The engine writes one ack at the END of the
 * sequence, so the UI cannot honestly claim to be on any particular step — inventing "cancelling…" then
 * "closing…" on a client-side timer would be a plausible narration of something we are not observing, and
 * would keep narrating after the engine had already failed. A counter that is merely true beats a
 * storyline that is usually true. Per-STEP reporting needs the engine to publish progress; until it does,
 * this says how long, not how far.
 *
 * Sits directly under HealthBanner and borrows its visual language deliberately: this is the same strip,
 * and an operator should not have to learn a second place to look.
 */
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getPositions } from "@/lib/api/client";
import { useCockpitStore } from "@/lib/framework/store";
import { elapsedLabel, isOverdue, staleBusyKeys } from "./busyLabel";

export function BusyBanner() {
  const busy = useCockpitStore((s) => s.busyCommands);
  const endCommand = useCockpitStore((s) => s.endCommand);
  // THE BANNER CLEARS ITS OWN (#374).
  //
  // `endCommand` was called from exactly one place — an effect inside `PositionDetail` — and that
  // component unmounts when the detail panel closes. A flatten started and then dismissed left a marker
  // with no code path able to remove it. Live 2026-08-19: "Flattening MNDY — 312s" still on screen after
  // `FL-94b07960` had filled 112/112 and the position was closed, surviving several engine restarts
  // because the marker never left the browser.
  //
  // Polled rather than pushed: this is a self-heal for a marker the detail may no longer be watching, and
  // it only has to be right within a few seconds. The Orders blotter remains authoritative.
  const { data: positions } = useQuery({
    queryKey: ["positions"],
    queryFn: getPositions,
    staleTime: 3_000,
    refetchInterval: 5_000,
  });
  // Ticks only while something is in flight — a permanent 1s interval on an idle cockpit is pure churn.
  const [now, setNow] = useState(() => Date.now());
  const active = Object.entries(busy);
  useEffect(() => {
    if (active.length === 0) return;
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, [active.length]);

  // Held keys in the same `strategy_id:instrument_id` shape the markers are keyed by. A position with
  // zero quantity is not held — a flatten that filled leaves the row present but flat for a moment.
  const heldKeys = new Set(
    (positions?.positions ?? [])
      .filter((p) => Number(p.quantity) !== 0)
      .map((p) => `${p.strategy_id}:${p.instrument_id}`),
  );
  const stale = staleBusyKeys(busy, heldKeys, now);
  useEffect(() => {
    // Guarded on length so an empty array does not re-enter set() on every tick. `stale` is derived, so
    // the join is what the effect actually depends on.
    if (stale.length > 0) stale.forEach(endCommand);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stale.join(","), endCommand]);

  if (active.length === 0) return null;

  return (
    <>
      {active.map(([key, cmd]) => {
        const overdue = isOverdue(cmd.startedAt, now);
        return (
          <div
            key={key}
            role="status"
            aria-live="polite"
            className={`border-b px-4 py-1.5 text-center font-mono text-[11px] font-semibold ${
              overdue
                ? "bg-amber-950/70 text-amber-200 border-amber-800/70"
                : "bg-sky-950/70 text-sky-200 border-sky-800/70"
            }`}
          >
            <span className="mr-1.5 inline-block animate-pulse">●</span>
            {cmd.label} — {elapsedLabel(cmd.startedAt, now)}
            {overdue ? " · longer than expected, check the Orders blotter" : ""}
          </div>
        );
      })}
    </>
  );
}
