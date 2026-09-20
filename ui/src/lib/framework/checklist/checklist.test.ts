import { describe, it, expect } from "vitest";
import { evaluateChecklist, type ChecklistDef, type ChecklistContext } from "./types";
import { registerChecklist, getChecklist, allChecklists } from "./registry";

const EMPTY_CTX: ChecklistContext = { weeklyBars: [], dailyBars: [] };

function fakeDef(overrides?: Partial<ChecklistDef>): ChecklistDef {
  return {
    id: "fake",
    name: "Fake Checklist",
    conditions: [
      { id: "a", label: "A", evaluate: () => true },
      { id: "b", label: "B", evaluate: () => false },
      { id: "c", label: "C", evaluate: () => null },
    ],
    tier: (score, total) => `${score}/${total}`,
    ...overrides,
  };
}

describe("evaluateChecklist", () => {
  it("scores only true conditions — null (unknown) never counts as pass or fail", () => {
    const result = evaluateChecklist(fakeDef(), EMPTY_CTX);
    expect(result.results).toEqual([true, false, null]);
    expect(result.score).toBe(1);
    expect(result.total).toBe(3);
    expect(result.unknown).toBe(1);
    expect(result.tier).toBe("1/3");
  });

  it("tier() receives the full results array, not just the tally — lets a config distinguish unknown from failed", () => {
    const def = fakeDef({
      conditions: [
        { id: "a", label: "A", evaluate: () => null },
        { id: "b", label: "B", evaluate: () => null },
      ],
      tier: (score, total, _veto, results) => (results.every((r) => r === null) ? "?" : `${score}/${total}`),
    });
    const result = evaluateChecklist(def, EMPTY_CTX);
    expect(result.unknown).toBe(2);
    expect(result.tier).toBe("?"); // NOT "0/2" — an all-unknown result must never read as a failing score
  });

  it("veto is independent of the score tally and passed through to tier()", () => {
    const def = fakeDef({
      veto: () => "weekly below cloud",
      tier: (score, total, veto) => (veto ? `VETO: ${veto}` : `${score}/${total}`),
    });
    const result = evaluateChecklist(def, EMPTY_CTX);
    expect(result.veto).toBe("weekly below cloud");
    expect(result.tier).toBe("VETO: weekly below cloud");
    expect(result.score).toBe(1); // veto doesn't suppress the raw score, just the tier label
  });

  it("no veto → null, not a false-y string", () => {
    const result = evaluateChecklist(fakeDef(), EMPTY_CTX);
    expect(result.veto).toBeNull();
  });
});

describe("checklist registry", () => {
  it("register/get/all round-trip — the mechanism is generic, not tied to any one methodology", () => {
    const def = fakeDef({ id: "registry-test-fake" });
    registerChecklist(def);
    expect(getChecklist("registry-test-fake")).toBe(def);
    expect(allChecklists()).toContain(def);
  });

  it("unknown id → undefined, not a throw", () => {
    expect(getChecklist("does-not-exist")).toBeUndefined();
  });
});
