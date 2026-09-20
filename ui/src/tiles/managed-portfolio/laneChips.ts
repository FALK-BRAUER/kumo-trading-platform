/**
 * Which lanes get a chip on the Portfolio tab (#1099).
 *
 * The chips used to be derived from ENGAGED CYCLES only, so a lane holding nothing had no chip and
 * the one screen the operator reads could not show that a lane had gone flat — QC345-003 on paper,
 * 2026-09-17: armed, sleeve 20,000, 0 held after five protective stop-outs, absent from the tab. The
 * chips are now the UNION of the lanes the registry says run on this node and the lanes the cycles
 * carry, keyed by the rendered label.
 *
 * "ON THE FRAME" IS NOT "IN THE REGISTRY". `/strategies` lists every lane the registry knows: on
 * paper 2026-09-18 that was seven rows, of which CRSISHORT-006 was not enabled on the instance. A
 * `CRSISHORT 0` chip would claim a lane the node does not run. The bridge's arm row is three-valued
 * (#997): `state: "unknown"` means the engine frame carries no arm state for the lane — a lane that
 * was never built, OR the manual lane, which is always live and never arms. The manual lane is told
 * apart by its cadence, which the registry declares; nothing else reads `arm.state` this way, so the
 * predicate lives here, once, and the seam test drives it through the rendered tile.
 *
 * AND "UNKNOWN" IS NOT "NOT HERE" UNLESS THE FRAME WAS READ. Every row reads `unknown` when the api
 * could not read the engine frame at all — a stale bridge, or the seconds at every deploy when a new
 * api reads no frame from an old engine (`api-and-engine-recreate-seconds-apart`). Under the
 * predicate alone that tick would drop EVERY chip and re-create the #1099 symptom as the fix. So the
 * registry is consulted only when `/strategies` says `engine_frame: "ok"` (cross-review, h2ho0jjf);
 * otherwise the chips are the cycles' lanes, exactly as before.
 *
 * KEYED BY LABEL, NOT ID. `strategyLabel("BridgeStrategy-…")` and `strategyLabel("MANUAL-001")` are
 * both `MANUAL`; a cycle on the legacy id beside the registry row would otherwise render two chips
 * reading `MANUAL n` under one label. The filter compares labels for the same reason.
 *
 * ABSENT REGISTRY → the cycles' lanes, exactly as before. Loading and failed are the same answer
 * here: the chips must never erase a lane the cycles prove is held, and the sleeve column already
 * renders `—` when the registry read failed, so the failure is not silent.
 */
import { strategyLabel } from "@/lib/framework/position";
import type { StrategyRow } from "@/lib/framework/useStrategyRows";

/** The engine frame names this lane's arm state, or the row is the manual lane. Meaningful ONLY
 *  when the frame was read — the caller checks `frame === "ok"` first. */
export function laneIsOnTheFrame(row: StrategyRow): boolean {
  if (!row.strategy_id) return false;
  if (row.cadence === "manual") return true;
  const state = row.arm?.state;
  return typeof state === "string" && state !== "unknown";
}

/** Sorted chip labels: the registry's on-frame lanes (when the frame was read) ∪ the cycles' lanes,
 *  by rendered label. */
export function chipLabels(
  registry: { rows: StrategyRow[] | undefined; frame: "ok" | "unreadable" | undefined },
  cycleLaneIds: readonly string[],
): string[] {
  const labels = new Set<string>();
  if (registry.frame === "ok") {
    for (const row of registry.rows ?? []) {
      if (laneIsOnTheFrame(row)) labels.add(strategyLabel(row.strategy_id as string));
    }
  }
  for (const id of cycleLaneIds) labels.add(strategyLabel(id));
  return [...labels].sort();
}
