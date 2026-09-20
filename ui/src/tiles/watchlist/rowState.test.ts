import { describe, it, expect, beforeEach } from "vitest";
import { reportRowState, clearRowState, subscribeRowState, getRowStateSnapshot } from "./rowState";

// The store is module-level (shared across tests in this file) — clear every symbol used so tests don't
// leak state into each other.
beforeEach(() => {
  for (const id of ["AAPL.XNAS", "MSFT.XNAS"]) clearRowState(id);
});

describe("reportRowState / getRowStateSnapshot", () => {
  it("round-trips a report", () => {
    reportRowState("AAPL.XNAS", { symbol: "AAPL", status: "HOLD", sortValue: 100, tier: "++", cloudPos: "above" });
    expect(getRowStateSnapshot().get("AAPL.XNAS")).toEqual({
      symbol: "AAPL",
      status: "HOLD",
      sortValue: 100,
      tier: "++",
      cloudPos: "above",
    });
  });

  it("an unreported symbol is absent, not a crash", () => {
    expect(getRowStateSnapshot().get("NOPE.XNAS")).toBeUndefined();
  });
});

describe("subscribeRowState", () => {
  it("notifies listeners on a real change", () => {
    let calls = 0;
    const unsubscribe = subscribeRowState(() => calls++);
    reportRowState("AAPL.XNAS", { symbol: "AAPL", status: "HOLD", sortValue: 1, tier: null, cloudPos: null });
    expect(calls).toBe(1);
    unsubscribe();
  });

  it("does NOT notify on a no-op report (identical state) — avoids a render storm from every row's own re-render", () => {
    reportRowState("AAPL.XNAS", { symbol: "AAPL", status: "HOLD", sortValue: 1, tier: null, cloudPos: null });
    let calls = 0;
    const unsubscribe = subscribeRowState(() => calls++);
    reportRowState("AAPL.XNAS", { symbol: "AAPL", status: "HOLD", sortValue: 1, tier: null, cloudPos: null }); // identical
    expect(calls).toBe(0);
    unsubscribe();
  });

  it("notifies when only tier or cloudPos changed — same shape as status/sortValue in the dedupe check", () => {
    reportRowState("AAPL.XNAS", { symbol: "AAPL", status: "HOLD", sortValue: 1, tier: "?", cloudPos: "in" });
    let calls = 0;
    const unsubscribe = subscribeRowState(() => calls++);
    reportRowState("AAPL.XNAS", { symbol: "AAPL", status: "HOLD", sortValue: 1, tier: "++", cloudPos: "above" });
    expect(calls).toBe(1);
    unsubscribe();
  });

  it("unsubscribe stops further notifications", () => {
    let calls = 0;
    const unsubscribe = subscribeRowState(() => calls++);
    unsubscribe();
    reportRowState("AAPL.XNAS", { symbol: "AAPL", status: "HOLD", sortValue: 1, tier: null, cloudPos: null });
    expect(calls).toBe(0);
  });
});

describe("getRowStateSnapshot reference identity (useSyncExternalStore contract)", () => {
  it("returns a NEW reference after a real change — useSyncExternalStore needs this to detect updates", () => {
    reportRowState("AAPL.XNAS", { symbol: "AAPL", status: "HOLD", sortValue: 1, tier: null, cloudPos: null });
    const before = getRowStateSnapshot();
    reportRowState("MSFT.XNAS", { symbol: "MSFT", status: "EXIT", sortValue: 2, tier: null, cloudPos: null });
    const after = getRowStateSnapshot();
    expect(after).not.toBe(before);
  });

  it("returns the SAME reference across calls when nothing changed — avoids an infinite re-render loop", () => {
    reportRowState("AAPL.XNAS", { symbol: "AAPL", status: "HOLD", sortValue: 1, tier: null, cloudPos: null });
    const a = getRowStateSnapshot();
    const b = getRowStateSnapshot();
    expect(a).toBe(b);
  });
});

describe("clearRowState", () => {
  it("removes an entry and notifies", () => {
    reportRowState("AAPL.XNAS", { symbol: "AAPL", status: "HOLD", sortValue: 1, tier: null, cloudPos: null });
    let calls = 0;
    const unsubscribe = subscribeRowState(() => calls++);
    clearRowState("AAPL.XNAS");
    expect(getRowStateSnapshot().has("AAPL.XNAS")).toBe(false);
    expect(calls).toBe(1);
    unsubscribe();
  });

  it("clearing an already-absent symbol is a harmless no-op — no notification, no throw", () => {
    let calls = 0;
    const unsubscribe = subscribeRowState(() => calls++);
    expect(() => clearRowState("NOPE.XNAS")).not.toThrow();
    expect(calls).toBe(0);
    unsubscribe();
  });
});
