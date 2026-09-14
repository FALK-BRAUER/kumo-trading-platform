import { describe, expect, it } from "vitest";

import type { SessionFrame } from "@/lib/framework/datasource/protocol";
import { exitReason, markerLabel, trailFor, trailLines } from "./trail";

const FRAME: SessionFrame = {
  session: "2026-08-10",
  decision: {
    summary: "TRADING: hold 8 · enter 8 · exit 2",
    reasons: {
      MET: "left the ranking",
      PRU: "gave back all of a 0.7% peak and is 1.5% below entry",
      CGAU: "entered: rank 1",
    },
  },
  trail: [
    { symbol: "MET", entry: 94.85, peak: 99.95, qty: 105, sessions_held: 1,
      quality: "reconstructed" },
    { symbol: "CGAU", entry: 21.06, peak: 21.06, qty: 459, sessions_held: 0, quality: "live" },
    { symbol: "OLD", entry: 50, peak: 50, qty: 10, sessions_held: 9, quality: "adopted" },
    { symbol: "SPIKE", entry: 10, peak: 12, qty: 10, sessions_held: 3, quality: "adopted" },
    { symbol: "SOLD", entry: 30, peak: 35, qty: 0, sessions_held: 4, quality: "reconstructed" },
  ],
};

describe("trailFor", () => {
  it("matches a bare symbol against a full instrument id", () => {
    expect(trailFor(FRAME, "MET.XNYS")?.entry).toBe(94.85);
    expect(trailFor(FRAME, "met")?.entry).toBe(94.85);
  });

  it("returns null for a symbol we do not hold", () => {
    expect(trailFor(FRAME, "NVDA.XNAS")).toBeNull();
  });

  it("REVIEW: keeps share classes apart", () => {
    // `split(".")[0]` collapsed BRK.B and BRK.A both to BRK, so one class's trail would draw its
    // levels on the other's chart.
    const frame: SessionFrame = {
      trail: [
        { symbol: "BRK.B", entry: 400, peak: 410, qty: 3, sessions_held: 1, quality: "live" },
        { symbol: "BRK.A", entry: 600000, peak: 610000, qty: 1, sessions_held: 1, quality: "live" },
      ],
    };
    expect(trailFor(frame, "BRK.B.XNYS")?.entry).toBe(400);
    expect(trailFor(frame, "BRK.A")?.entry).toBe(600000);
    expect(trailFor(frame, "BRK")).toBeNull();
  });
});

describe("trailLines", () => {
  it("draws entry and peak for an observed trail", () => {
    const lines = trailLines(FRAME, "MET.XNYS");
    expect(lines.map((l) => l.title)).toEqual(["entry", "peak"]);
    expect(lines[1].price).toBe(99.95);
  });

  it("REVIEW: draws no peak for an UNKNOWN provenance either", () => {
    // The guard is an allow-list. Written as `!== "adopted"` it fails OPEN — `ADOPTED`, a missing
    // field, or any future value would draw a fabricated level, which is the one thing this line
    // must never do.
    for (const quality of ["ADOPTED", "unknown", "", undefined as unknown as string]) {
      const frame: SessionFrame = {
        trail: [{ symbol: "Z", entry: 10, peak: 12, qty: 5, sessions_held: 1, quality }],
      };
      expect(trailLines(frame, "Z").map((l) => l.title)).toEqual(["entry"]);
    }
  });

  it("never draws a peak for an ADOPTED position", () => {
    // #197 B1: an adopted trail's peak was never observed. Drawing it would put a fabricated level
    // on the chart in the same visual language as a measured one.
    const lines = trailLines(FRAME, "SPIKE");
    expect(lines.map((l) => l.title)).toEqual(["entry"]);
    expect(JSON.stringify(lines)).not.toContain("12");
  });

  it("omits a peak equal to entry, which is not a peak", () => {
    expect(trailLines(FRAME, "CGAU").map((l) => l.title)).toEqual(["entry"]);
  });

  it("draws nothing when the position is not held", () => {
    expect(trailLines(FRAME, "NVDA")).toEqual([]);
  });

  it("draws nothing once the position is FLAT, even though the row survives", () => {
    // Observed live: MET and PRU sat at qty 105 and 81 in the trail while the broker held zero of
    // both. The runner retires the row at the next session's reconcile, so between the sell and the
    // following morning it is still there with the old entry and peak. Drawing them reads as
    // "still held".
    expect(trailLines(FRAME, "SOLD")).toEqual([]);
  });

  it("draws nothing before the frame arrives", () => {
    // A chart with no lines reads as "no position", which is true. A line at 0 reads as a level.
    expect(trailLines(null, "MET")).toEqual([]);
    expect(trailLines({}, "MET")).toEqual([]);
  });

  it("ignores unusable numbers rather than drawing a line at zero", () => {
    const bad: SessionFrame = {
      trail: [{ symbol: "X", entry: 0, peak: null, qty: 5, sessions_held: 1, quality: "live" }],
    };
    expect(trailLines(bad, "X")).toEqual([]);
  });
});

// 2026-08-10 12:00 UTC = 08:00 ET, and 2026-07-20 for the "old fill" case.
const TODAY_NS = Date.UTC(2026, 7, 10, 16, 0) * 1e6;
const OLD_NS = Date.UTC(2026, 6, 20, 16, 0) * 1e6;

describe("exitReason", () => {
  it("quotes the journal's own sentence", () => {
    expect(exitReason(FRAME, "PRU.XNYS")).toBe(
      "gave back all of a 0.7% peak and is 1.5% below entry",
    );
  });

  it("REVIEW: does not label a fill from an older session with the latest reason", () => {
    // The frame carries the LATEST decision, not necessarily today's, and chart fills are filtered
    // by instrument alone — so an exit from weeks ago would otherwise be annotated with this
    // morning's rationale. Attributing the strategy's reasoning to a trade it did not make is the
    // error this whole area keeps producing.
    expect(exitReason(FRAME, "PRU.XNYS", OLD_NS)).toBe("");
    expect(exitReason(FRAME, "PRU.XNYS", TODAY_NS)).toBe(
      "gave back all of a 0.7% peak and is 1.5% below entry",
    );
  });

  it("says nothing when the frame has no session at all", () => {
    expect(exitReason({ decision: { reasons: { PRU: "x" } } }, "PRU")).toBe("");
  });

  it("returns an ENTRY reason as empty, so it never lands on a sell marker", () => {
    // "entered: rank 1" describes an entry; annotating a sell with it would actively mislead.
    expect(exitReason(FRAME, "CGAU")).toBe("");
  });

  it("is empty for a symbol the decision never mentions", () => {
    expect(exitReason(FRAME, "NVDA")).toBe("");
    expect(exitReason(null, "MET")).toBe("");
  });
});

describe("markerLabel", () => {
  it("keeps price and size, and appends the reason", () => {
    expect(markerLabel("SELL", 105, 97.65, "left the ranking")).toBe(
      "SELL 105 @ 97.65 — left the ranking",
    );
  });

  it("is unchanged when there is no reason", () => {
    expect(markerLabel("BUY", 459, 21.01, "")).toBe("BUY 459 @ 21.01");
  });
});
