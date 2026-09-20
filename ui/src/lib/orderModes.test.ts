import { describe, expect, it } from "vitest";
import { toggleMode, type OrderModes } from "./orderModes";

const OFF: OrderModes = { extended: false, bracket: false, autoSel: false };

describe("toggleMode — at most one mode on", () => {
  it("turning one on from all-off enables only it", () => {
    expect(toggleMode(OFF, "autoSel")).toEqual({ extended: false, bracket: false, autoSel: true });
    expect(toggleMode(OFF, "extended")).toEqual({ extended: true, bracket: false, autoSel: false });
    expect(toggleMode(OFF, "bracket")).toEqual({ extended: false, bracket: true, autoSel: false });
  });

  it("turning a second on turns the first off (both directions)", () => {
    // AUTO on, then EXT → EXT on, AUTO off (the bug: previously AUTO stayed on)
    const auto = toggleMode(OFF, "autoSel");
    expect(toggleMode(auto, "extended")).toEqual({ extended: true, bracket: false, autoSel: false });
    // EXT on, then AUTO → AUTO on, EXT off
    const ext = toggleMode(OFF, "extended");
    expect(toggleMode(ext, "autoSel")).toEqual({ extended: false, bracket: false, autoSel: true });
    // BRACKET on, then AUTO → AUTO on, BRACKET off
    const br = toggleMode(OFF, "bracket");
    expect(toggleMode(br, "autoSel")).toEqual({ extended: false, bracket: false, autoSel: true });
    // AUTO on, then BRACKET → BRACKET on, AUTO off
    expect(toggleMode(auto, "bracket")).toEqual({ extended: false, bracket: true, autoSel: false });
  });

  it("turning the active mode off returns to all-off", () => {
    const auto = toggleMode(OFF, "autoSel");
    expect(toggleMode(auto, "autoSel")).toEqual(OFF);
  });

  it("never leaves two modes on for any pair/sequence", () => {
    const names = ["extended", "bracket", "autoSel"] as const;
    let s = OFF;
    for (const seq of [names, [...names].reverse(), ["autoSel", "extended", "bracket"] as const]) {
      for (const n of seq) {
        s = toggleMode(s, n);
        const onCount = Number(s.extended) + Number(s.bracket) + Number(s.autoSel);
        expect(onCount).toBeLessThanOrEqual(1);
      }
    }
  });
});
