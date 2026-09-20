/**
 * Feedback while a destructive command is in flight (#269 follow-up).
 *
 * 2026-08-17, after a flatten that ACTUALLY WORKED — NBIS closed, filled at 278.33, +$40.60:
 *
 *     "there was no proper feedback on flatten. The stock looked unchanged."
 *
 * Two separate defects produced that, and only one of them is cosmetic.
 *
 * THE REAL ONE: `useCommandStatus` gives up after 8 seconds and resolves to `unknown`, which by design
 * reports NOTHING rather than a false rejection. The exit path had just become much slower — it cancels
 * the resting stop, waits for the venue to confirm, waits for Alpaca to release the reserved shares, then
 * closes. Bounded at 6s + 10s + a round trip. His took 11s. So the ack was GUARANTEED to expire before
 * the engine answered, every single time a stop was resting, which is every position.
 *
 * THE COSMETIC ONE: nothing marked the position as busy, so the row he was watching was identical before,
 * during and after. A destructive control that looks inert is one an operator presses twice, and pressing
 * FLATTEN twice is how a position gets reversed instead of closed.
 */

import { describe, expect, it } from "vitest";

import { OVERDUE_MS, elapsedLabel, isOverdue, staleBusyKeys, STALE_BUSY_MS } from "./busyLabel";
import { CANCEL_THEN_ACT_TIMEOUT_MS } from "@/lib/framework/useCommandStatus";
import { useCockpitStore } from "@/lib/framework/store";

/** The engine's own bounded waits, from `engine_node.py`. Stated here so this test fails if the ack
 *  budget is ever trimmed back below what the engine can actually take. */
const ENGINE_CANCEL_CONFIRM_MS = 6_000;
const ENGINE_SHARE_RELEASE_MS = 10_000;
const ENGINE_WORST_CASE_MS = ENGINE_CANCEL_CONFIRM_MS + ENGINE_SHARE_RELEASE_MS;

describe("the ack budget", () => {
  it("EXCEEDS the engine's own worst case, or the ack expires before the answer arrives", () => {
    expect(CANCEL_THEN_ACT_TIMEOUT_MS).toBeGreaterThan(ENGINE_WORST_CASE_MS);
  });

  it("would have covered the 11s flatten that reported nothing", () => {
    // The measured case, not a hypothetical: NBIS on 2026-08-17, cancel at 16:01:34, fill at 16:01:45.
    const OBSERVED_MS = 11_000;
    expect(OBSERVED_MS).toBeGreaterThan(8_000); // the old default — this is why he saw nothing
    expect(CANCEL_THEN_ACT_TIMEOUT_MS).toBeGreaterThan(OBSERVED_MS);
  });

  it("leaves headroom rather than sitting just above the worst case", () => {
    // A budget of exactly 16s would expire on a venue having a slightly slow minute, reproducing the
    // silence intermittently — which is harder to diagnose than reproducing it always.
    expect(CANCEL_THEN_ACT_TIMEOUT_MS).toBeGreaterThanOrEqual(ENGINE_WORST_CASE_MS * 1.5);
  });
});

describe("the busy marker", () => {
  const KEY = "MANUAL-001:NBIS.XNAS";

  const reset = () => useCockpitStore.setState({ busyCommands: {} });

  it("makes the position busy so the ROW can change, not just the panel", () => {
    reset();
    useCockpitStore.getState().beginCommand(KEY, "flatten", "Flattening NBIS", 1_000);

    const busy = useCockpitStore.getState().busyCommands[KEY];
    expect(busy?.verb).toBe("flatten");
    expect(busy?.label).toBe("Flattening NBIS");
  });

  it("is keyed per POSITION, so one flatten does not freeze the whole book", () => {
    reset();
    useCockpitStore.getState().beginCommand(KEY, "flatten", "Flattening NBIS", 1_000);

    expect(useCockpitStore.getState().busyCommands["MOMENTUM-002:WPM.XNYS"]).toBeUndefined();
  });

  it("CLEARS, or the row stays PROCESSING forever and the next one is invisible against it", () => {
    reset();
    useCockpitStore.getState().beginCommand(KEY, "flatten", "Flattening NBIS", 1_000);
    useCockpitStore.getState().endCommand(KEY);

    expect(useCockpitStore.getState().busyCommands[KEY]).toBeUndefined();
  });

  it("tolerates a clear for something that was never busy", () => {
    // The ack effect fires on every terminal state, including for positions that never started a
    // command. Throwing there would take out the detail surface on an ordinary render.
    reset();
    expect(() => useCockpitStore.getState().endCommand("never:started")).not.toThrow();
    expect(useCockpitStore.getState().busyCommands).toEqual({});
  });
});

describe("what the banner says", () => {
  it("counts ELAPSED seconds — the one thing actually observed", () => {
    // The engine writes a single ack when the whole sequence finishes, so the UI cannot honestly claim
    // to be on any particular step. Narrating "cancelling…" then "closing…" off a client-side timer
    // would keep narrating after the engine had already failed.
    expect(elapsedLabel(10_000, 14_000)).toBe("4s");
    expect(elapsedLabel(10_000, 10_000)).toBe("0s");
  });

  it("never shows negative time when clocks disagree", () => {
    expect(elapsedLabel(10_000, 9_000)).toBe("0s");
  });

  it("turns overdue only AFTER the engine's own worst case has passed", () => {
    // Warning at 5s would cry wolf on every normal flatten — the engine is legitimately allowed 16s.
    expect(OVERDUE_MS).toBeGreaterThan(ENGINE_WORST_CASE_MS);
  });
});

/**
 * #374 — the banner has to clear its own markers.
 *
 * `endCommand` is called from exactly one place, an effect inside `PositionDetail`, and that component
 * unmounts when the detail panel closes. A flatten started and then dismissed leaves a marker nothing can
 * remove. Live on 2026-08-19: "Flattening MNDY — 312s" still on screen after `FL-94b07960` had filled
 * 112/112 and the position was closed — and it outlived several engine restarts, because the marker was
 * never on the engine at all.
 */
describe("stale busy markers clear themselves (#374)", () => {
  const KEY = "MANUAL-001:MNDY.XNAS";
  const OTHER = "MOMENTUM-002:FSM.XNYS";
  const T0 = 1_000_000;

  it("the fixture can express the bug: a held position is NOT cleared while in flight", () => {
    // Assert the fixture's own property first. If every key cleared, the assertions below would pass
    // against an implementation that simply wiped the store, which is the opposite of the fix.
    const busy = { [KEY]: { startedAt: T0 } };
    expect(staleBusyKeys(busy, new Set([KEY]), T0 + 5_000)).toEqual([]);
  });

  it("clears the marker once the position has left the book — the real MNDY case", () => {
    // The flatten filled and the position closed. That IS completion for a destructive command, whatever
    // happened to the ack, and it is the signal that fires in practice.
    const busy = { [KEY]: { startedAt: T0 } };
    expect(staleBusyKeys(busy, new Set([OTHER]), T0 + 5_000)).toEqual([KEY]);
  });

  it("keeps showing a slow flatten while its position is still held", () => {
    // 312 seconds is well past OVERDUE_MS, and the banner is SUPPOSED to keep showing it in amber while
    // the position is genuinely still there. Clearing on elapsed time alone would hide a real problem.
    const busy = { [KEY]: { startedAt: T0 } };
    expect(staleBusyKeys(busy, new Set([KEY]), T0 + 312_000)).toEqual([]);
  });

  it("gives up on a still-held marker only after the stale cap", () => {
    const busy = { [KEY]: { startedAt: T0 } };
    expect(staleBusyKeys(busy, new Set([KEY]), T0 + STALE_BUSY_MS - 1)).toEqual([]);
    expect(staleBusyKeys(busy, new Set([KEY]), T0 + STALE_BUSY_MS + 1)).toEqual([KEY]);
  });

  it("clears only the finished one when several are in flight", () => {
    const busy = { [KEY]: { startedAt: T0 }, [OTHER]: { startedAt: T0 } };
    expect(staleBusyKeys(busy, new Set([OTHER]), T0 + 1_000)).toEqual([KEY]);
  });

  it("an empty store yields nothing to clear", () => {
    expect(staleBusyKeys({}, new Set([KEY]), T0)).toEqual([]);
  });
});
