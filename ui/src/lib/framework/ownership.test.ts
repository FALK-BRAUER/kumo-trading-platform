import { describe, expect, it } from "vitest";

import { ownershipMessage } from "./ownership";

const v = (strategy_id: string, instrument_id: string, signed_qty: number) => ({
  strategy_id,
  instrument_id,
  signed_qty,
});

describe("ownershipMessage", () => {
  it("is silent when no lane is violating", () => {
    expect(ownershipMessage([])).toBe("");
    expect(ownershipMessage(undefined)).toBe("");
  });

  it("names the lane and the symbol, because the operator acts on the LANE", () => {
    const m = ownershipMessage([v("MOMENTUM-002", "WHD.XNYS", -28)]);
    expect(m).toContain("MOMENTUM-002");
    expect(m).toContain("WHD");
  });

  it("does NOT tell the operator to trust the broker's number", () => {
    // The whole point of a separate banner. On a mirrored pair the broker AGREES with the netted
    // cache, so drift's implicit remedy ("the broker is the anchor") is actively misleading here:
    // the total is right and both lane figures are wrong.
    const m = ownershipMessage([v("MOMENTUM-002", "WHD.XNYS", -28)]).toLowerCase();
    expect(m).not.toContain("broker holds");
    expect(m).toMatch(/held size|ownership|mis-?stat/);
  });

  it("elides a lane's symbols past a handful — the banner is one line, not a report", () => {
    const many = [
      v("MOMENTUM-002", "WHD.XNYS", -28),
      v("MOMENTUM-002", "CGAU.XNYS", -88),
      v("MOMENTUM-002", "VCTR.XNAS", -16),
      v("MOMENTUM-002", "HALO.XNAS", -18),
      v("EXTERNAL", "MRVL.XNAS", -14),
      v("EXTERNAL", "TOST.XNYS", -74),
    ];
    // Two lanes, but MOMENTUM-002 alone strands four symbols — the live 2026-08-30 shape. Lanes
    // rarely overflow; symbols do so immediately, which is the case that actually wraps the header.
    const m = ownershipMessage(many);
    expect(m).toMatch(/\+\d+ more/);
    expect(m.split("\n")).toHaveLength(1);
    expect(m).toContain("2 lanes");
  });

  it("counts the distinct lanes, since one lane can strand several symbols", () => {
    const m = ownershipMessage([
      v("MOMENTUM-002", "WHD.XNYS", -28),
      v("MOMENTUM-002", "CGAU.XNYS", -88),
    ]);
    expect(m).toContain("MOMENTUM-002");
    // One lane, two symbols — it must not read as two lanes.
    expect(m).not.toMatch(/2 lanes/);
  });
});

/**
 * THE BOOK'S "DISPUTED" BADGE HAS THREE STATES (#884, from the review of #883). With no engine
 * frame the api sends `ownership_violations: null`; `?? []` turned that into "no disputed positions"
 * on every branch of `classifyHealth`, so an UNKNOWN book rendered as a clean one — absence read as
 * clean, one layer above #859. Pure function, so the tile's decision is testable without a render.
 */
import { disputedBadge } from "./ownership";

describe("disputedBadge is three-state (#884)", () => {
  it("null (the engine did not say) is UNKNOWN, never 'none'", () => {
    const b = disputedBadge(null);
    expect(b.kind).toBe("unknown");
    expect(b.label).toMatch(/unknown/i);
  });
  it("an empty list is 'none' — asked, and clean", () => {
    expect(disputedBadge([]).kind).toBe("none");
  });
  it("a non-empty list is 'disputed' with the count", () => {
    const b = disputedBadge([{ strategy_id: "X-001", instrument_id: "A.XNYS", quantity: -1 } as never]);
    expect(b.kind).toBe("disputed");
    expect(b.count).toBe(1);
    expect(b.label).toMatch(/1 disputed/);
  });
  it("unknown and none must not render alike", () => {
    expect(disputedBadge(null).label).not.toBe(disputedBadge([]).label);
  });
});
