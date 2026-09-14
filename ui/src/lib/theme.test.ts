import { describe, it, expect } from "vitest";
import { themeModeSchema, resolveTheme, readStoredTheme, DEFAULT_THEME, THEME_BOOTSTRAP_SCRIPT } from "./theme";

describe("theme mode schema", () => {
  it("accepts the three modes, rejects anything else", () => {
    expect(themeModeSchema.safeParse("light").success).toBe(true);
    expect(themeModeSchema.safeParse("dark").success).toBe(true);
    expect(themeModeSchema.safeParse("system").success).toBe(true);
    expect(themeModeSchema.safeParse("blue").success).toBe(false);
    expect(themeModeSchema.safeParse(null).success).toBe(false);
  });
});

describe("resolveTheme", () => {
  it("passes light/dark through; system falls back to dark without a window", () => {
    expect(resolveTheme("light")).toBe("light");
    expect(resolveTheme("dark")).toBe("dark");
    expect(resolveTheme("system")).toBe("dark"); // node env → no matchMedia
  });
});

describe("readStoredTheme", () => {
  it("returns the default when there is no localStorage (SSR/node)", () => {
    expect(readStoredTheme()).toBe(DEFAULT_THEME);
  });
});

describe("bootstrap script", () => {
  it("is a self-invoking IIFE that toggles day/dark and can't throw", () => {
    expect(THEME_BOOTSTRAP_SCRIPT).toContain("classList.toggle('day'");
    expect(THEME_BOOTSTRAP_SCRIPT).toContain("classList.toggle('dark'");
    expect(THEME_BOOTSTRAP_SCRIPT).toContain("try{");
  });
});
