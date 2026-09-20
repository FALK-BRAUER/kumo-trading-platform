import { describe, it, expect } from "vitest";

/** The label a destructive control shows. Extracted as a pure string so it can be asserted. */
export function flattenLabel(symbol: string, quantity: number, long: boolean): string {
  return `Slide to FLATTEN ${symbol} — ${long ? "sell" : "buy back"} ${quantity} shares`;
}

describe("flatten label", () => {
  it("never renders a bare number that could be read as an HTTP status", () => {
    // 2026-08-14: "Flatten seems to return 404." BETA held 404 shares and the button read
    // "Slide to FLATTEN BETA (404)". On a control that sells the position, a number that looks like an
    // error code is worse than unclear — it invites the reader to think the action already failed.
    const label = flattenLabel("BETA", 404, true);
    expect(label).not.toMatch(/\(\d+\)/);
    expect(label).toContain("404 shares");
  });

  it("says which direction the trade goes", () => {
    expect(flattenLabel("BETA", 404, true)).toContain("sell 404 shares");
    expect(flattenLabel("BETA", 404, false)).toContain("buy back 404 shares");
  });
});
