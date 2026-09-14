/**
 * Wire ids a position may be moved INTO — from the LIVE registry, not a compile-time list (#783).
 *
 * WHY THIS EXISTS. `config/strategies.ts` hardcoded the catalog, so `transferTargets()` returned
 * `["MANUAL-001"]` and could never return anything else. BCTROT-004, QC345-003 and TECHIVOL-005 were
 * absent entirely; MOMENTUM carried tag `001` when the real StrategyId is `MOMENTUM-002`. staging's
 * `TRT.AMEX EXTERNAL +25` — a real unclaimed broker holding — was therefore unassignable to the lane
 * that should run it, even after the backend (#781) began accepting every registered lane.
 *
 * The constant's own docstring said "which strategies exist is a deployment decision". That was true
 * when there was one deployment. There are now two with different lane sets, and the API already
 * serves the truth per instance. A compile-time constant cannot be right for both.
 *
 * DEGRADES LOUDLY. When the registry cannot be read this returns NO targets and says why, rather
 * than falling back to a plausible-looking `["MANUAL-001"]`. A fallback here would put the position
 * somewhere nobody chose, and reading as if that were the answer is the whole defect.
 */

import { useStrategyRows } from "@/lib/framework/useStrategyRows";

export interface TransferTargets {
  /** Wire ids (`MANUAL-001`, `BCTROT-004`, …). Empty when the registry could not be read. */
  targets: string[];
  /** Three states, never two: the registry answered, has not answered yet, or could not be read. */
  state: "ready" | "loading" | "unavailable";
  /** Operator-facing reason when `state === "unavailable"`. */
  reason?: string;
}

export function useTransferTargets(): TransferTargets {
  // A SELECTOR over the one `/strategies` read (#888 review): a transfer target must come from the same
  // snapshot as the sleeve and cadence rendered beside it, so there is one query key, not one per hook.
  const { rows, failed } = useStrategyRows();
  if (failed) {
    return {
      targets: [],
      state: "unavailable",
      reason:
        "the strategy registry could not be read, so the lanes a position may be moved into are " +
        "unknown — this is not the same as there being only one",
    };
  }
  if (rows === undefined) {
    return { targets: [], state: "loading" };
  }
  return {
    targets: rows
      .map((r) => String(r.strategy_id ?? ""))
      .filter((id) => id.length > 0)
      .sort(),
    state: "ready",
  };
}
