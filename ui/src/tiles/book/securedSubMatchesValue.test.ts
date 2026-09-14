/**
 * The SECURED sub-line must not contradict the number beside it (#786).
 *
 * MEASURED on test-alpaca, 30 held:
 *
 *     SECURED · NOW   $925.85
 *     30 of 30 protected · none above entry
 *
 * Both cannot be true. `securedValue` accumulates ONLY `gain > 0` — `if (gain > 0 && qty > 0) value
 * += gain * qty` (managed-portfolio/books.ts) — so a non-zero value PROVES stops sit above entry.
 *
 * The `sub` ternary fell through on `covered > 0` alone and never inspected `value`, while the
 * `title` immediately below it guarded correctly on `value === 0`. Two derivations of one fact,
 * disagreeing, on a SAFETY cell — the exact shape `books.ts` already warns about for `isProtected`:
 * "a tally and a filter that disagree about what protected means is a safety claim that is wrong on
 * one of the two screens showing it."
 *
 * The copy itself was right when written (#345: a fully protected book showed a bare "$0.00" and
 * needed a reason). The REASON was correct; the guard on when to say it was never added.
 */

import { describe, expect, it } from "vitest";

import { securedSub } from "./securedSub";

const HELD = 30;

describe("the SECURED sub-line", () => {
  it("does NOT claim 'none above entry' when there IS locked-in gain", () => {
    // The live case. Killed by dropping the `value === 0` guard.
    const sub = securedSub({ value: 925.85, covered: 30, naked: 0, unprotectable: 0 }, HELD);
    expect(sub ?? "").not.toContain("above entry");
  });

  it("still explains a fully protected book that has locked in NOTHING", () => {
    // #345's original case, which the copy exists for. Killed by removing the branch entirely.
    const sub = securedSub({ value: 0, covered: 30, naked: 0, unprotectable: 0 }, HELD);
    expect(sub).toBe("30 of 30 protected · none above entry");
  });

  it("agrees with the tooltip on the one fact they both state", () => {
    // The two-derivations rule: bind them to the SAME predicate, then prove they cannot diverge.
    for (const value of [0, 0.004, 1, 925.85]) {
      const s = securedSub({ value, covered: 30, naked: 0, unprotectable: 0 }, HELD);
      const titleSaysNoneAboveEntry = value === 0;
      expect((s ?? "").includes("above entry")).toBe(titleSaysNoneAboveEntry);
    }
  });

  it("reports unprotected and unpriceable ahead of either, unchanged", () => {
    expect(securedSub({ value: 0, covered: 8, naked: 2, unprotectable: 3 }, 13))
      .toBe("2 of 13 unprotected · 3 unpriceable");
    expect(securedSub({ value: 0, covered: 10, naked: 0, unprotectable: 3 }, 13))
      .toBe("3 of 13 unpriceable — cannot be protected");
    expect(securedSub({ value: 999, covered: 10, naked: 3, unprotectable: 0 }, 13))
      .toBe("3 of 13 unprotected");
  });

  it("says nothing when there is nothing to say", () => {
    expect(securedSub({ value: 925.85, covered: 0, naked: 0, unprotectable: 0 }, 0)).toBeUndefined();
  });
});
