"use client";

/**
 * useCommandStatus (#39) — poll a UI command's engine ack. A command POST returns `ok` = ENQUEUED plus a
 * `command_id`; this resolves that to the engine's real outcome so the UI never reports "placed" on a mere
 * enqueue. Polls until `accepted`/`rejected`, or its OWN timeout → `unknown` (a lost ack must never read as a
 * false `rejected`). After `accepted`, the Orders blotter is the authoritative lifecycle.
 */
import { useEffect, useState } from "react";
import { getCommandStatus, type CommandState } from "@/lib/api/client";

/**
 * How long to wait on a command that CANCELS RESTING ORDERS before acting (#269).
 *
 * THE DEFAULT 8s IS NOT ENOUGH FOR THESE AND SILENTLY WASN'T. The engine's exit path is a sequence of
 * bounded waits, and the ack is only written at the end of it:
 *
 *   _await_reducing_orders_clear   6s   the venue confirming the cancel
 *   _await_shares_available       10s   Alpaca actually releasing the reserved shares
 *   + the close order's own round trip
 *
 * So ~17s worst case. the operator's NBIS flatten on 2026-08-17 took 11s, the ack expired at 8s, and the hook
 * resolved to `unknown` — which by design reports NOTHING rather than a false rejection. The exit had in
 * fact completed and filled. He saw an unchanged screen: "there was no proper feedback on flatten."
 *
 * 30s leaves headroom over the worst case rather than sitting just above the typical one. Getting this
 * wrong is not a cosmetic failure: `unknown` is indistinguishable from "nothing happened".
 */
export const CANCEL_THEN_ACT_TIMEOUT_MS = 30_000;

export function useCommandStatus(
  commandId: string | null,
  timeoutMs = 8000,
): { state: CommandState; error: string | null } {
  const [state, setState] = useState<CommandState>("pending");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!commandId) {
      setState("pending");
      setError(null);
      return;
    }
    let alive = true;
    const started = Date.now();
    setState("pending");
    setError(null);

    const tick = async () => {
      if (!alive) return;
      try {
        const s = await getCommandStatus(commandId);
        if (!alive) return;
        if (s.status === "accepted" || s.status === "rejected") {
          setState(s.status);
          setError(s.error ?? null);
          return;
        }
      } catch {
        /* transient fetch error — keep polling until the timeout */
      }
      if (Date.now() - started > timeoutMs) {
        if (alive) setState("unknown"); // lost ack / engine slow → check the blotter, NOT a false reject
        return;
      }
      setTimeout(tick, 500);
    };
    tick();
    return () => {
      alive = false;
    };
  }, [commandId, timeoutMs]);

  return { state, error };
}
