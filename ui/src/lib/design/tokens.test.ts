import { describe, it, expect } from "vitest";
import { THEME_TOKENS, chartColors, themeCssVars } from "./tokens";

describe("design tokens", () => {
  it("defines both themes with the same keys", () => {
    expect(Object.keys(THEME_TOKENS.night).sort()).toEqual(Object.keys(THEME_TOKENS.day).sort());
  });

  it("every token is a hex colour", () => {
    for (const theme of ["night", "day"] as const) {
      for (const [k, v] of Object.entries(THEME_TOKENS[theme])) {
        expect(v, `${theme}.${k}`).toMatch(/^#[0-9a-f]{6}$/i);
      }
    }
  });

  it("chartColors resolves bull/bear from the active theme", () => {
    expect(chartColors("night").up).toBe(THEME_TOKENS.night.bull);
    expect(chartColors("night").down).toBe(THEME_TOKENS.night.bear);
    expect(chartColors("day").up).toBe(THEME_TOKENS.day.bull);
  });

  it("themeCssVars prefixes every token with --ds-", () => {
    const vars = themeCssVars("night");
    expect(vars["--ds-bull"]).toBe(THEME_TOKENS.night.bull);
    expect(Object.keys(vars).every((k) => k.startsWith("--ds-"))).toBe(true);
  });
});
