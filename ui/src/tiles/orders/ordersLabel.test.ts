import { describe, expect, it } from "vitest";

/**
 * The order-type label is derived in OrdersTile; this pins the rule that broke.
 * A TRAILING stop carries no limit price and — until the venue sets one — no trigger price either, so a
 * price-only rule fell through to "MKT". A resting "MKT · GTC" reads as a market order left working
 * overnight, which is alarming and wrong: it was PEAK's trailing stop, doing exactly its job.
 */
function priceLabel(order: {
  order_type: string;
  price: number | null;
  trigger_price: number | null;
}): string {
  const fmt = (v: number | null | undefined) => (v == null ? "" : v.toFixed(2));
  const trailing = order.order_type.startsWith("TRAILING_STOP");
  return trailing
    ? `TRAIL${order.trigger_price != null ? ` ${fmt(order.trigger_price)}` : ""}`
    : order.price != null && order.trigger_price != null
      ? `STPLMT ${fmt(order.trigger_price)}/${fmt(order.price)}`
      : order.price != null
        ? `LMT ${fmt(order.price)}`
        : order.trigger_price != null
          ? `STP ${fmt(order.trigger_price)}`
          : "MKT";
}

describe("order type label", () => {
  it("does NOT call a trailing stop a market order", () => {
    // The live SAP order: PEAK's trail, accepted GTC, no prices populated yet.
    expect(priceLabel({ order_type: "TRAILING_STOP_MARKET", price: null, trigger_price: null })).toBe("TRAIL");
  });

  it("shows the trail's trigger once the venue sets one", () => {
    expect(priceLabel({ order_type: "TRAILING_STOP_MARKET", price: null, trigger_price: 204.75 })).toBe("TRAIL 204.75");
  });

  it("still labels a real market order MKT", () => {
    expect(priceLabel({ order_type: "MARKET", price: null, trigger_price: null })).toBe("MKT");
  });

  it("leaves limit, stop and stop-limit untouched", () => {
    expect(priceLabel({ order_type: "LIMIT", price: 192.68, trigger_price: null })).toBe("LMT 192.68");
    expect(priceLabel({ order_type: "STOP_MARKET", price: null, trigger_price: 175.46 })).toBe("STP 175.46");
    expect(priceLabel({ order_type: "STOP_LIMIT", price: 10, trigger_price: 9 })).toBe("STPLMT 9.00/10.00");
  });
});
