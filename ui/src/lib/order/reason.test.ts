import { describe, expect, it } from "vitest";
import { humanizeOrderReason } from "./reason";

describe("humanizeOrderReason", () => {
  it("extracts the Alpaca message + market price from the raw envelope", () => {
    const raw =
      'Alpaca POST /v2/orders → 422: {"code":42210000,"market_price":"24.11","message":"stop price must be greater than current price","stop_price":"23.9"}';
    expect(humanizeOrderReason(raw)).toBe(
      "Stop price must be greater than current price (mkt $24.11)",
    );
  });

  it("handles an envelope without a market price", () => {
    const raw = 'Alpaca POST /v2/orders → 403: {"code":40310000,"message":"insufficient buying power"}';
    expect(humanizeOrderReason(raw)).toBe("Insufficient buying power");
  });

  it("passes through an already-human engine reason unchanged", () => {
    expect(humanizeOrderReason("Engine disarmed — order not submitted")).toBe(
      "Engine disarmed — order not submitted",
    );
  });

  it("passes through a non-JSON body unchanged", () => {
    const raw = "Alpaca POST /v2/orders → 500: Internal Server Error";
    expect(humanizeOrderReason(raw)).toBe(raw);
  });

  it("returns null for empty input", () => {
    expect(humanizeOrderReason(null)).toBeNull();
    expect(humanizeOrderReason("")).toBeNull();
  });
});
