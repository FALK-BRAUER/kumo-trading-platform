import { describe, it, expect } from "vitest";
import { laneWindowRealized } from "./windowDelta";

describe("laneWindowRealized (#662)", () => {
  const frame = {
    realized_periods: {
      "1D": { by_strategy: { "TECHIVOL-005": 221.63, "MOMENTUM-002": -76.69 } },
      "1W": { by_strategy: { "TECHIVOL-005": -57.47 } },
    },
  };

  it("reads the SWEEP bucket — the ET-day number, not the since-boot session figure", () => {
    // The #653 pair, live 2026-08-28: native session −59.98 vs ET-day +221.63 after a 12:12 ET
    // restart. The tile must render the window's number under a window label.
    expect(laneWindowRealized(frame, "TECHIVOL-005", "1D")).toBeCloseTo(221.63);
    expect(laneWindowRealized(frame, "MOMENTUM-002", "1D")).toBeCloseTo(-76.69);
  });

  it("a lane absent from a PRESENT window realized zero there — 0, not unknown", () => {
    expect(laneWindowRealized(frame, "MOMENTUM-002", "1W")).toBe(0);
  });

  it("an absent window or pre-sweep frame is UNKNOWN, never zero", () => {
    expect(laneWindowRealized(frame, "TECHIVOL-005", "3M")).toBeNull();
    expect(laneWindowRealized({}, "TECHIVOL-005", "1D")).toBeNull();
    expect(laneWindowRealized(null, "TECHIVOL-005", "1D")).toBeNull();
  });
});
