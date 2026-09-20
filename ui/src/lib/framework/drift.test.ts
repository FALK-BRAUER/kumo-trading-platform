import { describe, expect, it } from "vitest";
import { classifyDrift, driftMessage, type DriftEntry } from "./drift";

const entry = (symbol: string, broker_qty: number, platform_qty: number): DriftEntry => ({
  symbol,
  broker_qty,
  platform_qty,
});

describe("classifyDrift", () => {
  it("calls it understated when only the broker holds it", () => {
    expect(classifyDrift(entry("AAPL", 100, 0))).toBe("understated");
  });

  it("calls it phantom when only the cockpit holds it", () => {
    expect(classifyDrift(entry("HSBC", 0, -93))).toBe("phantom");
  });

  it("calls it a mismatch when both hold it in different size", () => {
    expect(classifyDrift(entry("MET", 105, 60))).toBe("mismatch");
  });

  // Our backend cannot emit this (it only reports a symbol when the sides differ), but classifying it
  // as a direction would print "SYMBOL 0" — the exact bug this module removes.
  it("calls a zero-zero row in sync rather than guessing a direction", () => {
    expect(classifyDrift(entry("NOP", 0, 0))).toBe("insync");
  });
});

describe("driftMessage", () => {
  it("is empty when there is no drift", () => {
    expect(driftMessage([])).toBe("");
    expect(driftMessage(null)).toBe("");
    expect(driftMessage(undefined)).toBe("");
  });

  it("names the broker quantity when the broker holds what the cockpit does not show", () => {
    expect(driftMessage([entry("AAPL", 100, 0)])).toBe(
      "Broker sync drift — held at the broker but not shown: AAPL 100. Manage at your broker.",
    );
  });

  // The regression this file exists for: the old banner printed `broker_qty` for every entry, so the
  // real 6 Aug phantom rendered as "positions held but not shown: HSBC 0" — the opposite of the truth,
  // quoting a quantity nobody held.
  it("names the cockpit quantity, and the right direction, for a phantom", () => {
    expect(driftMessage([entry("HSBC", 0, -93)])).toBe(
      "Broker sync drift — shown but not held at the broker: HSBC -93. Manage at your broker.",
    );
  });

  it("never renders a phantom as a broker holding of 0", () => {
    expect(driftMessage([entry("HSBC", 0, -93)])).not.toContain("HSBC 0");
  });

  it("reports both quantities on a mismatch, and which way it cuts", () => {
    expect(driftMessage([entry("MET", 105, 60)])).toBe(
      "Broker sync drift — quantity mismatch: MET broker 105 vs cockpit 60 (broker holds more). Manage at your broker.",
    );
  });

  it("says cockpit shows more when the cockpit is the larger side", () => {
    expect(driftMessage([entry("MET", 60, 105)])).toContain("(cockpit shows more)");
  });

  // A side disagreement is not a size disagreement — "broker 50 vs cockpit -50" is the most dangerous
  // shape and reads as neither long nor short without being named.
  it("calls out opposite sides rather than describing them as a size difference", () => {
    expect(driftMessage([entry("FIG", 50, -50)])).toContain("(opposite sides)");
  });

  it("says nothing at all when every row is in sync", () => {
    expect(driftMessage([entry("NOP", 0, 0)])).toBe("");
  });

  it("renders fractional share counts without float artifacts", () => {
    expect(driftMessage([entry("FRAC", 0.1 + 0.2, 0)])).toContain("FRAC 0.3");
    expect(driftMessage([entry("FRAC", 0.1 + 0.2, 0)])).not.toContain("0.30000000000000004");
  });

  it("does not render tiny quantities in scientific notation", () => {
    expect(driftMessage([entry("TINY", 0.0000001, 0)])).not.toContain("e-");
  });

  it("puts unshown broker risk before phantom risk when both are present", () => {
    const msg = driftMessage([entry("HSBC", 0, -93), entry("AAPL", 100, 0)]);
    expect(msg).toBe(
      "Broker sync drift — held at the broker but not shown: AAPL 100; " +
        "shown but not held at the broker: HSBC -93. Manage at your broker.",
    );
  });

  // "…" alone hides whether one symbol or two hundred were dropped, and a dropped one may carry the
  // largest drift — so the remainder is counted.
  it("counts the symbols it drops instead of trailing off", () => {
    const many = ["A", "B", "C", "D", "E"].map((s) => entry(s, 10, 0));
    const msg = driftMessage(many);
    expect(msg).toContain("A 10, B 10, C 10, D 10, +1 more");
    expect(msg).not.toContain("E 10");
  });

  it("counts a large remainder accurately", () => {
    const many = Array.from({ length: 204 }, (_, i) => entry(`S${i}`, 10, 0));
    expect(driftMessage(many)).toContain("+200 more");
  });

  it("elides each direction independently", () => {
    const drift = [
      ...["A", "B", "C", "D", "E"].map((s) => entry(s, 10, 0)),
      ...["V", "W", "X", "Y", "Z"].map((s) => entry(s, 0, -5)),
    ];
    const msg = driftMessage(drift);
    expect(msg).toContain("A 10, B 10, C 10, D 10, +1 more");
    expect(msg).toContain("V -5, W -5, X -5, Y -5, +1 more");
  });
});
