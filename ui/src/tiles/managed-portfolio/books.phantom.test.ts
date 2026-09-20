/**
 * #808 item 4 — a phantom UNCLAIMED row carries no money.
 *
 * On 2026-09-09 the UNCLAIMED book stood at +$271.86 on four rows the broker held none of (CRM 10,
 * HALO 10, WDAY 11, PATH 192 — #807's reconciliation-minted counterparties). That figure entered the
 * header's standing unrealized, DEPLOYED, and the lane cells. `venue_qty` (from #811) is the broker's
 * answer: 0 means "holds none" and the row is a phantom; null means "not asked yet", which is NOT the
 * same and stays counted — absence is not permission either way.
 */
import { describe, expect, it } from "vitest";

import { attribute, deployedValue, type ExternalLike } from "./books";

const phantom: ExternalLike = { realized_pnl: "0.00 USD", unrealized_pl: 228.48, market_value: 2691.84, venue_qty: 0 };
const backed: ExternalLike = { realized_pnl: "0.00 USD", unrealized_pl: -3.93, market_value: 1490.24, venue_qty: 8 };
const unconfirmed: ExternalLike = { realized_pnl: "0.00 USD", unrealized_pl: 21.05, market_value: 2484.0, venue_qty: null };

describe("the fixture can express the defect", () => {
  it("the phantom carries money the account does not own", () => {
    expect(phantom.venue_qty).toBe(0);
    expect(phantom.unrealized_pl).not.toBe(0);
    expect(phantom.market_value).toBeGreaterThan(0);
  });
});

describe("attribute — the UNCLAIMED book", () => {
  it("excludes a phantom's P&L and counts it", () => {
    const { unclaimed } = attribute([], [phantom, backed]);
    expect(unclaimed.unrealized).toBeCloseTo(-3.93, 2);
    expect(unclaimed.held).toBe(1);
    expect(unclaimed.phantom).toBe(1);
  });
  it("keeps an UNCONFIRMED row — not told is not 'holds none'", () => {
    const { unclaimed } = attribute([], [unconfirmed]);
    expect(unclaimed.unrealized).toBeCloseTo(21.05, 2);
    expect(unclaimed.phantom).toBe(0);
  });
  it("the 2026-09-09 book: 271.86 shown, 0.00 real", () => {
    const rows: ExternalLike[] = [
      { unrealized_pl: 21.05, venue_qty: 0 }, // CRM
      { unrealized_pl: 26.26, venue_qty: 0 }, // HALO
      { unrealized_pl: 228.48, venue_qty: 0 }, // PATH (broker held 126 under a lane, not this row)
      { unrealized_pl: -3.93, venue_qty: 0 }, // WDAY
    ];
    const { unclaimed } = attribute([], rows);
    expect(rows.reduce((s, r) => s + (r.unrealized_pl ?? 0), 0)).toBeCloseTo(271.86, 2); // what the panel showed
    expect(unclaimed.unrealized).toBe(0);
    expect(unclaimed.phantom).toBe(4);
  });
});

describe("deployedValue — a phantom is not deployed capital", () => {
  it("excludes the phantom's market value", () => {
    const d = deployedValue([], [phantom, backed]);
    expect(d.value).toBeCloseTo(1490.24, 2);
    expect(d.unmarked).toBe(0);
  });
});
