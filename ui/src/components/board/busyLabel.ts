/**
 * What the in-flight banner says, as plain functions (#269 follow-up).
 *
 * Split out of `BusyBanner.tsx` for the same reason `swipeGuard.ts` is split out of its component: these
 * are the parts with a right and a wrong answer, and they can be tested against real numbers instead of
 * through a render.
 */

/** Past this, the wait exceeds every bounded wait in the engine's exit path (6s confirming the cancel +
 *  10s waiting for the shares + a round trip) and something is actually wrong. Warning sooner would cry
 *  wolf on every normal flatten, because the engine is legitimately allowed that long. */
export const OVERDUE_MS = 20_000;

/**
 * Seconds since the command was sent.
 *
 * ELAPSED IS SHOWN BECAUSE IT IS WHAT WE ACTUALLY KNOW. The engine writes one ack at the END of the
 * sequence, so the UI cannot honestly claim to be on any particular step. Narrating "cancelling…" then
 * "closing…" off a client-side timer would be a plausible account of something we are not observing, and
 * it would keep narrating confidently after the engine had already failed. A counter that is merely true
 * beats a storyline that is usually true.
 *
 * Clamped at zero: the start instant comes from the browser and the comparison instant from a ticking
 * interval, and a backwards clock must not render "-1s".
 */
export function elapsedLabel(startedAt: number, now: number): string {
  return `${Math.max(0, Math.round((now - startedAt) / 1000))}s`;
}

export function isOverdue(startedAt: number, now: number): boolean {
  return now - startedAt > OVERDUE_MS;
}

/**
 * A busy marker is client state, and nothing outside the position detail ever cleared it (#374).
 *
 * `endCommand` is called from ONE place: an effect inside `PositionDetail`. That component unmounts when
 * the detail panel closes, so a flatten started and then dismissed leaves its marker in the store with no
 * code path left that can remove it. Measured 2026-08-19: "Flattening MNDY — 312s" still on screen after
 * `FL-94b07960` had filled 112/112 and the position was closed, and it survived several engine restarts
 * because the marker never left the browser.
 *
 * So the banner clears its own, on evidence rather than on a component's lifetime:
 *
 *   GONE — the position is no longer held. For a destructive command that IS completion; a flatten whose
 *   position has left the book has finished, whatever the ack did. This is the honest signal and it is
 *   what fires in practice.
 *
 *   STALE — still held, but far past any bounded wait in the engine. Not evidence of completion, so the
 *   cap is deliberately generous: `OVERDUE_MS` already warns the operator at 20s and the banner keeps
 *   showing it, amber, for the whole window. Only past this does the marker stop being informative and
 *   start being furniture. The Orders blotter is authoritative either way.
 *
 * Not a timeout on the command — the engine is untouched. This only decides how long a browser keeps
 * claiming something is in flight when it can no longer see it.
 */
export const STALE_BUSY_MS = 10 * 60 * 1000;

export function staleBusyKeys(
  busy: Record<string, { startedAt: number }>,
  heldKeys: ReadonlySet<string>,
  now: number,
): string[] {
  return Object.entries(busy)
    .filter(([key, cmd]) => !heldKeys.has(key) || now - cmd.startedAt > STALE_BUSY_MS)
    .map(([key]) => key);
}
