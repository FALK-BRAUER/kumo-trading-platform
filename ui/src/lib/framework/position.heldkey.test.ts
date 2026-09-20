/** Which position a portfolio row opens. A row is an instrument GROUP, so this must never guess. */
import { describe, expect, it } from "vitest";

import { heldPositionKey } from "./position";

const cycle = (strategy_id: string, state: string) => ({
  strategy_id,
  instrument_id: "FIG.XNYS",
  state,
});

describe("heldPositionKey", () => {
  it("names the position when exactly one cycle is held", () => {
    expect(heldPositionKey([cycle("MANUAL-001", "HELD")])).toBe("MANUAL-001:FIG.XNYS");
  });

  it("ignores cycles that are not held", () => {
    expect(heldPositionKey([cycle("MANUAL-001", "HELD"), cycle("MOMENTUM-001", "CLOSED")])).toBe(
      "MANUAL-001:FIG.XNYS",
    );
  });

  it("returns null when nothing is held — an ARMED row has no position to manage yet", () => {
    expect(heldPositionKey([cycle("MANUAL-001", "ARMED")])).toBeNull();
  });

  it("returns null on an empty group", () => {
    expect(heldPositionKey([])).toBeNull();
  });

  it("returns null when two strategies hold it — picking either would be a guess", () => {
    expect(heldPositionKey([cycle("MANUAL-001", "HELD"), cycle("MOMENTUM-001", "HELD")])).toBeNull();
  });
});
