import { describe, expect, it } from "vitest";
import { stripEmpty } from "./settingsForm";

describe("stripEmpty", () => {
  it("drops null / undefined / empty-string (cleared fields → backend default)", () => {
    expect(stripEmpty({ feed: "sip", n: null, s: "", u: undefined, keep: 0 })).toEqual({ feed: "sip", keep: 0 });
  });
  it("keeps false and 0 (valid values, not 'empty')", () => {
    expect(stripEmpty({ flag: false, count: 0 })).toEqual({ flag: false, count: 0 });
  });
  it("passes a full valid payload through unchanged", () => {
    expect(stripEmpty({ feed: "iex" })).toEqual({ feed: "iex" });
  });
});
