import { describe, expect, it } from "vitest";
import {
  bracketProtectionExpires,
  bracketTif,
  buildBracketPayload,
  buildOrderPayload,
} from "./payload";

const base = { instrumentId: "AAPL.XNAS", action: "BUY" as const, priceNum: 190.5, tif: "day", sharesNum: 10 };

describe("buildOrderPayload", () => {
  it("market: no price/trigger, not extended", () => {
    const p = buildOrderPayload({ ...base, orderType: "market", extended: false });
    expect(p).toMatchObject({ order_type: "market", quantity: 10, side: "BUY", extended_hours: false });
    expect(p.price).toBeUndefined();
    expect(p.trigger_price).toBeUndefined();
  });

  it("limit: price set, no trigger", () => {
    const p = buildOrderPayload({ ...base, orderType: "limit", extended: false });
    expect(p.price).toBe(190.5);
    expect(p.trigger_price).toBeUndefined();
    expect(p.time_in_force).toBe("day");
  });

  it("stop: the price field becomes trigger_price, not price", () => {
    const p = buildOrderPayload({ ...base, orderType: "stop", extended: false });
    expect(p.trigger_price).toBe(190.5);
    expect(p.price).toBeUndefined();
  });

  it("extended-hours coerces to a DAY limit (Alpaca rule) regardless of the chosen type/tif", () => {
    const p = buildOrderPayload({ ...base, orderType: "market", extended: true, tif: "gtc" });
    expect(p.order_type).toBe("limit");
    expect(p.price).toBe(190.5); // now a limit → price rides
    expect(p.time_in_force).toBe("day");
    expect(p.extended_hours).toBe(true);
  });
});

describe("buildBracketPayload", () => {
  const b = { ...base, stopNum: 185, targetNum: 200 };

  it("market entry: no entry price, stop+target carried", () => {
    const p = buildBracketPayload({ ...b, orderType: "market" });
    expect(p.entry_order_type).toBe("market");
    expect(p.price).toBeUndefined();
    expect(p.stop_trigger).toBe(185);
    expect(p.target_price).toBe(200);
  });

  it("limit entry: entry price rides", () => {
    const p = buildBracketPayload({ ...b, orderType: "limit" });
    expect(p.entry_order_type).toBe("limit");
    expect(p.price).toBe(190.5);
  });

  // NOTE: bracket entry is typed "market" | "limit" — a stop entry is a compile error by design (no silent
  // coercion). The UI coerces stop→market when BRACKET is enabled; the type forbids passing "stop" here.
});

import { bracketGeoOk, stopTriggerOk } from "./payload";

describe("stopTriggerOk", () => {
  it("non-stop order always ok", () => {
    expect(stopTriggerOk("BUY", "limit", 0, 0)).toBe(true);
    expect(stopTriggerOk("SELL", "market", 0, 100)).toBe(true);
  });
  it("BUY stop must trigger ABOVE ref, SELL BELOW", () => {
    expect(stopTriggerOk("BUY", "stop", 105, 100)).toBe(true); // above → ok
    expect(stopTriggerOk("BUY", "stop", 95, 100)).toBe(false); // below → fires instantly
    expect(stopTriggerOk("SELL", "stop", 95, 100)).toBe(true); // below → ok
    expect(stopTriggerOk("SELL", "stop", 105, 100)).toBe(false);
  });
  it("needs positive trigger + ref", () => {
    expect(stopTriggerOk("BUY", "stop", 105, 0)).toBe(false);
    expect(stopTriggerOk("BUY", "stop", 0, 100)).toBe(false);
  });
});

describe("bracketGeoOk", () => {
  it("BUY needs stop < entry < target", () => {
    expect(bracketGeoOk("BUY", 100, 95, 110)).toBe(true);
    expect(bracketGeoOk("BUY", 100, 105, 110)).toBe(false); // stop above entry
    expect(bracketGeoOk("BUY", 100, 95, 98)).toBe(false); // target below entry
  });
  it("SELL needs target < entry < stop", () => {
    expect(bracketGeoOk("SELL", 100, 105, 90)).toBe(true);
    expect(bracketGeoOk("SELL", 100, 95, 90)).toBe(false); // stop below entry
  });
  it("any non-positive leg fails", () => {
    expect(bracketGeoOk("BUY", 0, 95, 110)).toBe(false);
    expect(bracketGeoOk("BUY", 100, 0, 110)).toBe(false);
  });
});

/**
 * #315. Alpaca hands the parent bracket's `time_in_force` to BOTH child legs and offers no per-leg field,
 * so this one value decides how long the PROTECTIVE STOP lives. The call site hardcoded `"day"`.
 *
 * Measured on a live instance paper book, 2026-08-15: every stop the #239 backstop placed rested `gtc`; the one
 * `day` protection was a dialogue bracket, whose stop expires at 16:00 and leaves the position naked until
 * the backstop's first regular-hours tick — reopening the exact overnight gap #239 was built to close.
 */
describe("bracketTif", () => {
  it("a MARKET entry rests GTC — the entry fills instantly, so the legs get protection that outlives the session", () => {
    expect(bracketTif("market")).toBe("gtc");
  });

  it("a LIMIT entry stays DAY — GTC would buy protection by leaving an unfilled BUY resting indefinitely", () => {
    // The trade-off is real and deliberate: an entry that can fill days later, on a name the operator has
    // stopped watching, is a worse hazard than a stop that expires with the backstop standing behind it.
    expect(bracketTif("limit")).toBe("day");
  });

  it("says so when protection expires, so the ticket can warn BEFORE the slide", () => {
    expect(bracketProtectionExpires("limit")).toBe(true);
    expect(bracketProtectionExpires("market")).toBe(false);
  });

  // The bug was not in the helper — it was at the CALL SITE, which passed a literal. Pin the value that
  // actually reaches the wire, because a correct helper nobody calls is exactly the shape that shipped
  // five defects here with a green suite.
  it("the payload a MARKET bracket puts on the wire carries gtc, not day", () => {
    const p = buildBracketPayload({
      instrumentId: "NBIS.XNAS",
      action: "BUY",
      orderType: "market",
      priceNum: 0,
      sharesNum: 29,
      stopNum: 260,
      targetNum: 310.61,
      tif: bracketTif("market"),
    });
    expect(p.time_in_force).toBe("gtc");
  });

  // The live NBIS bracket, reconstructed: 29 shares, stop 260, target 310.61, market entry. It went out
  // DAY, and both legs expire at the close.
  it("regression: the NBIS bracket of 2026-08-15 would now rest GTC", () => {
    expect(bracketTif("market")).not.toBe("day");
  });
});
