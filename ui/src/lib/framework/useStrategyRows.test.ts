/** The sleeve and the cadence on one lane cell come from ONE snapshot of `/strategies` (#888 review).
 *
 * Two hooks with two query keys were two caches of one payload on two refresh clocks; a cell could then
 * show a sleeve from one and a cadence from the other. Both hooks are selectors over `useStrategyRows`.
 * Pinned by feeding that one source rows with values no default could produce and reading both
 * selectors off it — a hook that fetched on its own would not see 12345 or `weekly`.
 */
import { describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ rows: undefined as unknown[] | undefined, failed: false }));

vi.mock("@/lib/framework/useStrategyRows", () => ({
  useStrategyRows: () => ({ rows: host.rows, failed: host.failed }),
}));

import { useLaneCadence } from "./useLaneCadence";
import { useSleeves } from "./useSleeves";
import { useTransferTargets } from "./useTransferTargets";

describe("useSleeves and useLaneCadence select from one /strategies read", () => {
  it("both reflect the same rows", () => {
    host.rows = [
      { strategy_id: "QC345-003", target: 12345, cadence: "weekly", next_rebalance: "2026-09-14" },
      { strategy_id: "MOMENTUM-002", target: null, cadence: "daily", next_rebalance: null },
      { target: 1 }, // no id: dropped by both, never keyed as ""
    ];
    expect(useSleeves()).toEqual({ "QC345-003": 12345, "MOMENTUM-002": null });
    expect(useLaneCadence()).toEqual({
      "QC345-003": { cadence: "weekly", next_rebalance: "2026-09-14" },
      "MOMENTUM-002": { cadence: "daily", next_rebalance: null },
    });
    expect(useTransferTargets()).toEqual({ targets: ["MOMENTUM-002", "QC345-003"], state: "ready" });
  });

  it("transfer targets keep their three states off the same read: loading, unavailable, ready", () => {
    host.rows = undefined;
    host.failed = false;
    expect(useTransferTargets().state).toBe("loading");
    host.failed = true;
    expect(useTransferTargets().state).toBe("unavailable");
    expect(useTransferTargets().targets).toEqual([]);
    host.failed = false;
  });

  it("an absent payload is an EMPTY map for both — `—` and `cadence —` per lane, never a default", () => {
    host.rows = undefined;
    expect(useSleeves()).toEqual({});
    expect(useLaneCadence()).toEqual({});
  });

  it("a row whose fields are the wrong type reads as unknown, not as a value", () => {
    host.rows = [{ strategy_id: "X-001", target: "20000", cadence: 3, next_rebalance: 20261001 }];
    expect(useSleeves()).toEqual({ "X-001": null });
    expect(useLaneCadence()).toEqual({ "X-001": { cadence: null, next_rebalance: null } });
  });
});
