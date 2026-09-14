import { describe, expect, it } from "vitest";
import { focusFromInstrumentId, parseInstrumentId } from "./focus";

describe("parseInstrumentId", () => {
  it("splits symbol.venue", () => {
    expect(parseInstrumentId("AAPL.XNAS")).toEqual({ symbol: "AAPL", venue: "XNAS" });
  });
  it("handles no venue", () => {
    expect(parseInstrumentId("AAPL")).toEqual({ symbol: "AAPL", venue: "" });
  });
});

describe("focusFromInstrumentId", () => {
  it("builds identity + context, DTO-neutral (no position fields)", () => {
    const f = focusFromInstrumentId("JNJ.XNYS", { tab: "portfolio", strategy_id: "MANUAL" }, "Johnson & Johnson");
    expect(f).toEqual({
      instrument_id: "JNJ.XNYS",
      symbol: "JNJ",
      venue: "XNYS",
      name: "Johnson & Johnson",
      context: { tab: "portfolio", strategy_id: "MANUAL" },
    });
    // no qty/side/pnl leaked in
    expect(Object.keys(f)).not.toContain("quantity");
  });
  it("defaults name to empty (watchlist rows have no name)", () => {
    expect(focusFromInstrumentId("SMH.XNAS", { tab: "watch" }).name).toBe("");
  });
});
