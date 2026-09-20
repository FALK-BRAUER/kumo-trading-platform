/** What the lane cell says about a lane's cadence (#888). Pure; rendered by `BookCell`'s sub-line.
 *
 * Three states, never two:
 * - a rotation cadence (`monthly`, or anything the registry adds) names itself and its next date;
 * - `daily` and `manual` say nothing — daily is what the liveness count already assumes, manual has no
 *   schedule the alarm could be wrong about;
 * - UNKNOWN (no row, or a row without the field) says `cadence —`. It must not render like daily,
 *   because "read as daily" is the misreading that took a monthly lane for a dead one.
 */
import type { LaneCadence } from "@/lib/framework/useLaneCadence";

export interface CadenceNote {
  text: string;
  title: string;
}

const SILENT = new Set(["daily", "manual"]);

export function cadenceNote(row: LaneCadence | undefined): CadenceNote | null {
  const cadence = row?.cadence ?? null;
  if (cadence === null) {
    return {
      text: "cadence —",
      title: "This lane's cadence could not be read from /strategies — unknown, not daily.",
    };
  }
  if (SILENT.has(cadence)) return null;
  const next = row?.next_rebalance ?? "—";
  const title =
    cadence === "monthly"
      ? "Monthly rotation: decides on the first session of each month and is exit-only on every other " +
        "session, so the sessions-since-decision count is expected to climb between rebalances."
      : `${cadence} rotation; next scheduled decision ${next}.`;
  return { text: `${cadence} · next ${next}`, title };
}
