import { describe, it, expect } from "vitest";
import { positionKey, strategyLabel } from "./position";

describe("positionKey", () => {
  it("keys by strategy + instrument (same symbol, two strategies → distinct keys)", () => {
    expect(positionKey({ strategy_id: "MOMENTUM-001", instrument_id: "MPC.XNYS" })).toBe("MOMENTUM-001:MPC.XNYS");
    const a = positionKey({ strategy_id: "MOMENTUM-001", instrument_id: "AAPL.XNAS" });
    const b = positionKey({ strategy_id: "MANUAL-001", instrument_id: "AAPL.XNAS" });
    expect(a).not.toBe(b); // the tile row and the detail must resolve the SAME position
  });
});

describe("strategyLabel", () => {
  it("strips the Nautilus tag and aliases the dev bridge strategy", () => {
    expect(strategyLabel("MOMENTUM-001")).toBe("MOMENTUM");
    expect(strategyLabel("MANUAL-001")).toBe("MANUAL");
    expect(strategyLabel("BridgeStrategy-42")).toBe("MANUAL");
  });
});
