import { describe, it, expect } from "vitest";
import { pnlToneClass } from "./pnl";

describe("pnlToneClass", () => {
  it("positive → bull, negative → bear", () => {
    expect(pnlToneClass(12.5)).toBe("text-status-bull");
    expect(pnlToneClass(-3)).toBe("text-status-bear");
  });
  it("flat / null / undefined → muted", () => {
    expect(pnlToneClass(0)).toBe("text-t2");
    expect(pnlToneClass(null)).toBe("text-t2");
    expect(pnlToneClass(undefined)).toBe("text-t2");
  });
});
