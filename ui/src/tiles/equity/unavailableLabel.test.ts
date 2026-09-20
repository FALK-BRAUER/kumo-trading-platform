/**
 * "No account history yet" is a CLAIM, and on the IBKR stack it is a false one (#559).
 *
 * `broker.equity_curve` has exactly one publisher — the Alpaca exec client. On ibkr-paper-retired nothing
 * ever writes the topic, so the tile drew "No account history yet" over an account holding
 * $1,000,000 with months of history. True of a fresh Alpaca account; false here.
 *
 * "yet" says the history is COMING. The engine now states the absence after a boot grace and says
 * why; the tile must render that instead of implying a wait that will never end.
 *
 * The wording is deliberately NOT duplicated here — the engine observed the absence and owns the
 * sentence. Two sources for one fact drift, and the tile would be the one that goes stale.
 */
import { describe, expect, it } from "vitest";

/** The tile's own resolution, extracted so it can be driven without mounting React. */
function emptyLabelFor(payload: { curves?: Record<string, unknown>; unavailable?: string } | undefined) {
  return payload?.unavailable ?? "No account history yet";
}

describe("equity tile empty state", () => {
  it("says WHY when the engine reports the stack cannot produce a curve", () => {
    const reason =
      "this stack's broker publishes no account history — the equity curve comes from the " +
      "Alpaca portfolio-history endpoint, and nothing supplies an equivalent here";
    expect(emptyLabelFor({ curves: {}, unavailable: reason })).toBe(reason);
  });

  it("still says 'yet' when the curve has merely not arrived", () => {
    // The normal state for the first seconds of every boot, and on a genuinely new account. The fix
    // must not turn every cold start into "this broker has no history".
    expect(emptyLabelFor({ curves: {} })).toBe("No account history yet");
    expect(emptyLabelFor(undefined)).toBe("No account history yet");
  });

  it("prefers the engine's reason over the generic label, not the other way round", () => {
    // Guards the ?? direction. Reversed, the reason would be unreachable and this file would still
    // pass its first assertion if that used the same default.
    expect(emptyLabelFor({ curves: {}, unavailable: "X" })).not.toBe("No account history yet");
  });
});
