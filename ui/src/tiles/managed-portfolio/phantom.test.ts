import { describe, expect, it } from "vitest";

import { movable, phantomLabel, phantomState } from "./phantom";

describe("phantomState — three states, never two", () => {
  it("0 is a phantom: the broker answered and holds none", () => {
    expect(phantomState(0)).toBe("phantom");
  });
  it("a quantity is backed", () => {
    expect(phantomState(126)).toBe("backed");
    expect(phantomState(8)).toBe("backed");
  });
  it("null and undefined are unconfirmed — not phantom, not backed", () => {
    expect(phantomState(null)).toBe("unconfirmed");
    expect(phantomState(undefined)).toBe("unconfirmed");
  });
});

describe("phantomLabel — the sub-line says what the broker said", () => {
  it("a phantom is named and loses the move affordance", () => {
    const label = phantomLabel("phantom", "reconciled");
    expect(label).toContain("phantom");
    expect(label).toContain("broker holds 0");
    expect(label).not.toContain("tap to move");
  });
  it("unconfirmed keeps the origin, says the broker has not answered, and offers no move", () => {
    expect(phantomLabel("unconfirmed", "reconciled")).toBe("reconciled — broker unconfirmed, cannot be moved yet");
    expect(phantomLabel("unconfirmed", "reconciled")).not.toContain("tap to move");
  });
  it("backed keeps the ordinary affordance", () => {
    expect(phantomLabel("backed", "reconciled")).toBe("reconciled — tap to move to a strategy");
  });
});

describe("movable — only a backed row may open the move detail", () => {
  it("backed yes; phantom and unconfirmed no", () => {
    expect(movable("backed")).toBe(true);
    expect(movable("phantom")).toBe(false);
    expect(movable("unconfirmed")).toBe(false);
  });
});
