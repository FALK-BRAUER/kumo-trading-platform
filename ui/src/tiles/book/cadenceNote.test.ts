/** The lane cell must SAY when a lane is not daily (#888).
 *
 * On 2026-09-11 QC345-003 read `last decided 2026-09-01 · 7 sessions since` and was taken for a lane
 * dead for a week. It is a MONTHLY rotation doing exactly what it should. The liveness alarm (#349) is
 * calibrated for daily lanes; beside a monthly lane it needs the cadence and the next rebalance date, or
 * it trains its reader to ignore it.
 *
 * Three states, never two: monthly says so with a date; daily says nothing (the norm the alarm assumes);
 * UNKNOWN — the `/strategies` read failed or an older api has no such field — says "cadence —" rather
 * than silently reading as daily, which is precisely the #888 misreading rendered by omission.
 */
import { describe, expect, it } from "vitest";

import { cadenceNote } from "./cadenceNote";

describe("cadenceNote", () => {
  it("names a monthly lane with its next rebalance", () => {
    const note = cadenceNote({ cadence: "monthly", next_rebalance: "2026-10-01" });
    expect(note?.text).toBe("monthly · next 2026-10-01");
    expect(note?.title).toContain("first session of each month");
  });

  it("a monthly lane whose next date is unknown still says monthly — the date is a dash, not omitted", () => {
    const note = cadenceNote({ cadence: "monthly", next_rebalance: null });
    expect(note?.text).toBe("monthly · next —");
  });

  it("a daily lane renders nothing: daily is what the liveness count already assumes", () => {
    expect(cadenceNote({ cadence: "daily", next_rebalance: null })).toBeNull();
  });

  it("a manual lane renders nothing — it has no schedule the alarm could be wrong about", () => {
    expect(cadenceNote({ cadence: "manual", next_rebalance: null })).toBeNull();
  });

  it("UNKNOWN is its own state: no row, or a row without the field, renders 'cadence —' and never as daily", () => {
    // The whole point: a fetch failure must not render like a daily lane, because that is the reading
    // that was wrong. A mutant returning null for `undefined` passes the daily test and fails here.
    expect(cadenceNote(undefined)?.text).toBe("cadence —");
    expect(cadenceNote({ cadence: null, next_rebalance: null })?.text).toBe("cadence —");
    expect(cadenceNote(undefined)?.title).toMatch(/could not be read/);
  });

  it("an unrecognised cadence is shown verbatim rather than mapped to a known one", () => {
    // A cadence added upstream tomorrow ("weekly") must reach the eye, not be swallowed by a switch
    // with a default branch.
    expect(cadenceNote({ cadence: "weekly", next_rebalance: "2026-09-14" })?.text).toBe("weekly · next 2026-09-14");
  });
});
