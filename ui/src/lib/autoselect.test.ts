import { describe, expect, it } from "vitest";
import { autoSelectOrderType, sessionPhase } from "./autoselect";
import { AUTO_SELECT } from "@/config/orderTicket";

const cfg = AUTO_SELECT;

// A Date at a given ET wall-clock time (use a fixed winter date → EST = UTC-5).
const etTime = (h: number, m: number): Date => new Date(Date.UTC(2026, 0, 14, h + 5, m)); // Jan → EST

describe("sessionPhase", () => {
  it("classifies the trading day (EST)", () => {
    expect(sessionPhase(etTime(3, 0), cfg)).toBe("closed");
    expect(sessionPhase(etTime(7, 0), cfg)).toBe("extended"); // pre-market
    expect(sessionPhase(etTime(9, 35), cfg)).toBe("open"); // first 15m
    expect(sessionPhase(etTime(12, 0), cfg)).toBe("mid");
    expect(sessionPhase(etTime(15, 55), cfg)).toBe("close"); // last 15m
    expect(sessionPhase(etTime(18, 0), cfg)).toBe("extended"); // post-market
    expect(sessionPhase(etTime(21, 0), cfg)).toBe("closed");
  });
});

describe("autoSelectOrderType", () => {
  it("extended/closed → resting limit, never market", () => {
    expect(autoSelectOrderType({ entryStyle: "take", spreadPct: 0.0001, volRel: 0.01, phase: "extended", cfg }).orderType).toBe("limit");
    expect(autoSelectOrderType({ entryStyle: "take", spreadPct: 0.0001, volRel: 0.01, phase: "closed", cfg }).orderType).toBe("limit");
  });

  it("breakout → stop on a tight book; fast tape → stop-market reason", () => {
    const calm = autoSelectOrderType({ entryStyle: "breakout", spreadPct: 0.0002, volRel: 0.01, phase: "mid", cfg });
    expect(calm.orderType).toBe("stop");
    const hot = autoSelectOrderType({ entryStyle: "breakout", spreadPct: 0.0002, volRel: 0.05, phase: "mid", cfg });
    expect(hot.orderType).toBe("stop");
    expect(hot.reason).toMatch(/fast tape/);
  });

  it("breakout on a wide/unknown book → resting limit, NOT stop-market (no unbounded fill)", () => {
    expect(autoSelectOrderType({ entryStyle: "breakout", spreadPct: 0.05, volRel: 0.01, phase: "mid", cfg }).orderType).toBe("limit");
    expect(autoSelectOrderType({ entryStyle: "breakout", spreadPct: null, volRel: 0.05, phase: "mid", cfg }).orderType).toBe("limit");
  });

  it("crossed spread (< 0) is treated as wide → limit, not market", () => {
    const r = autoSelectOrderType({ entryStyle: "take", spreadPct: -0.001, volRel: 0.01, phase: "mid", cfg });
    expect(r.orderType).toBe("limit");
  });

  it("pullback → resting limit", () => {
    expect(autoSelectOrderType({ entryStyle: "pullback", spreadPct: 0.0002, volRel: 0.01, phase: "mid", cfg }).orderType).toBe("limit");
  });

  it("take + tight spread + mid session → market", () => {
    expect(autoSelectOrderType({ entryStyle: "take", spreadPct: 0.0003, volRel: 0.01, phase: "mid", cfg }).orderType).toBe("market");
  });

  it("take + wide/unknown spread → resting limit (IEX safety)", () => {
    expect(autoSelectOrderType({ entryStyle: "take", spreadPct: 0.05, volRel: 0.01, phase: "mid", cfg }).orderType).toBe("limit");
    expect(autoSelectOrderType({ entryStyle: "take", spreadPct: null, volRel: 0.01, phase: "mid", cfg }).orderType).toBe("limit");
  });

  it("take + tight spread but open window → limit not market", () => {
    expect(autoSelectOrderType({ entryStyle: "take", spreadPct: 0.002, volRel: 0.01, phase: "open", cfg }).orderType).toBe("limit");
  });
});
