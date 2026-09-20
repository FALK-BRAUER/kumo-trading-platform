import { describe, it, expect } from "vitest";
import { instanceBadgeLabel } from "./instanceBadge";

describe("instanceBadgeLabel (#610)", () => {
  it("passes a real instance name through", () => {
    expect(instanceBadgeLabel("ibkr-paper-retired")).toBe("ibkr-paper-retired");
    expect(instanceBadgeLabel("alpaca-paper")).toBe("alpaca-paper");
  });

  it("EMPTY STRING is unset — Docker bakes an omitted ARG to \"\", not undefined", () => {
    // The exact bug class that blanked the whole UI in #21/config.ts: `??` keeps "", so an instance
    // deployed without INSTANCE= would render an empty badge — invisible, which is indistinguishable
    // from "not deployed with the fix". Unset must be LOUD, not blank.
    expect(instanceBadgeLabel("")).toBe("UNNAMED");
  });

  it("undefined (local dev, no docker) is also UNNAMED", () => {
    expect(instanceBadgeLabel(undefined)).toBe("UNNAMED");
  });
});
