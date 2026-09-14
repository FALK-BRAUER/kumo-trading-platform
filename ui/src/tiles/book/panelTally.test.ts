import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { deltaUnrealized } from "./panelIdentity";

/**
 * #310 — the BOOK panel shows DEPLOYED, CASH and LIQUIDATION together, so a reader adds them. It has to
 * add up.
 *
 * The operator did exactly that and it did not reconcile:
 *
 *     DEPLOYED + CASH = 99,978.32 + (-208.42) = 99,769.90
 *     LIQUIDATION                              = 99,764.62
 *
 * DEPLOYED was derived from OUR trade projection while CASH and LIQUIDATION came from the broker, so the
 * three used different prices and different instants. Alpaca's own figures are exact.
 */
function deployedFor(account: { long_market_value?: number } | undefined, derivedValue: number) {
  const lmv = account?.long_market_value;
  return lmv != null && lmv > 0 ? lmv : derivedValue;
}

describe("BOOK panel arithmetic", () => {
  it("uses the broker's market value so DEPLOYED + CASH === LIQUIDATION exactly", () => {
    // Measured on the live paper account 2026-08-14.
    const account = { long_market_value: 100_052.04, cash: -208.42, equity: 99_843.62 };
    const deployed = deployedFor(account, 99_978.32); // the projection-derived figure disagrees
    expect(deployed).toBe(100_052.04);
    expect(Number((deployed + account.cash).toFixed(2))).toBe(account.equity);
  });

  it("falls back to the projection when the broker offers no market value", () => {
    // A synthetic or non-Alpaca node has no account snapshot; showing nothing would be worse than
    // showing our own estimate, which is what the tile did before.
    expect(deployedFor(undefined, 99_978.32)).toBe(99_978.32);
    expect(deployedFor({}, 99_978.32)).toBe(99_978.32);
    expect(deployedFor({ long_market_value: 0 }, 99_978.32)).toBe(99_978.32);
  });

  it("the projection-derived figure is the one that can UNDER-count", () => {
    // It is built from held cycles with a mark; a position without one is silently missing, which is why
    // the tile suffixed it with "+". The broker has no such gap.
    const derived = 99_978.32;
    const broker = 100_052.04;
    expect(broker).toBeGreaterThan(derived);
  });
});

/**
 * #345 — the panel must satisfy the identity its own subtitle states.
 *
 * NET says "realized + change in unrealized over the window". The UNREALIZED cell rendered the standing
 * LEVEL, so the two named terms did not produce the named total. Same family as #310 above: numbers
 * printed side by side get added, so they have to add up.
 *
 * These use the live paper account, 2026-08-19, off the `ui:state:equity_curve` plane and
 * `realized_periods`.
 */
describe("REALIZED + Δ UNREALIZED === NET (#345)", () => {
  // NET per window, and the FIFO-matched realized for the same window. The standing unrealized was
  // 1,176.79 at this snapshot — one number for every window, which is exactly why it cannot be the term
  // in a per-window identity.
  const WINDOWS = [
    { period: "1W", net: 2922.86, realized: 2650.21 },
    { period: "1M", net: 1556.25, realized: 819.57 },
    { period: "3M", net: 847.61, realized: 138.5 },
    { period: "1D", net: -679.77, realized: 0 },
  ];
  const STANDING_UNREALIZED = 1176.79;

  it("the fixture can express the bug: the standing level is NOT the window's delta", () => {
    // Assert the fixture's own property before the invariance. If the level happened to equal the delta
    // in this snapshot, every assertion below would pass against the OLD implementation too and pin
    // nothing at all.
    for (const w of WINDOWS) {
      expect(deltaUnrealized(w.net, w.realized)).not.toBeCloseTo(STANDING_UNREALIZED, 2);
    }
  });

  it("the identity holds for every window", () => {
    for (const w of WINDOWS) {
      const delta = deltaUnrealized(w.net, w.realized)!;
      expect(Number((w.realized + delta).toFixed(2))).toBeCloseTo(w.net, 2);
    }
  });

  it("the delta MOVES with the window — it is a real quantity, not a constant", () => {
    // 272.65 / 736.68 / 709.11 / -679.77. A cell that showed the same number under every tab is what
    // made the old panel look broken; this one has to earn its place by varying.
    const deltas = WINDOWS.map((w) => deltaUnrealized(w.net, w.realized)!);
    expect(new Set(deltas.map((d) => d.toFixed(2))).size).toBe(4);
    expect(deltas[0]).toBeCloseTo(272.65, 2);
    expect(deltas[1]).toBeCloseTo(736.68, 2);
    expect(deltas[2]).toBeCloseTo(709.11, 2);
    expect(deltas[3]).toBeCloseTo(-679.77, 2);
  });

  it("adding the OLD pair overshoots NET — the reading the operator actually made", () => {
    // Kept as the regression's own arithmetic so whoever changes this cell sees what it cost.
    expect(2650.21 + STANDING_UNREALIZED).toBeCloseTo(3827.0, 2);
    expect(2650.21 + STANDING_UNREALIZED).not.toBeCloseTo(2922.86, 2);
  });

  it("a negative delta survives — a window can book gains while the book gives some back", () => {
    expect(deltaUnrealized(-679.77, 0)).toBeCloseTo(-679.77, 2);
    expect(deltaUnrealized(100, 250)).toBeCloseTo(-150, 2);
  });

  it("unknown is unknown, never zero", () => {
    // "No broker P&L for this window yet" and "the mark did not move" are different claims, and the
    // second is far stronger. A dash is the honest render.
    expect(deltaUnrealized(null, 100)).toBeNull();
    expect(deltaUnrealized(100, null)).toBeNull();
    expect(deltaUnrealized(undefined, undefined)).toBeNull();
    expect(deltaUnrealized(NaN, 100)).toBeNull();
    expect(deltaUnrealized(100, Infinity)).toBeNull();
  });

  it("a genuinely flat window is 0.00 and must survive", () => {
    expect(deltaUnrealized(500, 500)).toBe(0);
  });
});

/**
 * THE WIRING, not the helper (#345).
 *
 * `deltaUnrealized` was correct the moment it was written, and a green test on it says nothing about
 * whether the panel calls it. The defect was never in a helper — it was in WHICH VALUE the cell
 * rendered, which is a property of the call site. This project has no rendered-component tests, and
 * CLAUDE.md's own tally is five production breaks in one day where the unit was right and the wiring
 * was wrong, every one of them behind a green suite.
 *
 * So: scan the source. The cell that names unrealized must render the DERIVED delta, and the standing
 * level must not be the headline value of a cell sitting under a period selector.
 */
describe("BookTile actually renders the delta (#345)", () => {
  const SRC = readFileSync(join(import.meta.dirname, "BookTile.tsx"), "utf8");
  const code = SRC.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

  it("the scan reads the real component and it is not empty", () => {
    // The fixture's own property first: a path typo would make every assertion below vacuous.
    expect(SRC.length).toBeGreaterThan(2000);
    expect(code).toContain("periodNet");
  });

  it("MEASURES the delta and derives NET from it — the reverse of what #345 shipped", () => {
    // REVERSED BY #596, deliberately. This used to assert `deltaUnrealized(net, realized)`, which was
    // correct for #345 (the cell had been rendering the standing LEVEL) and wrong in a way #345 could
    // not see: with Δ defined as `NET − REALIZED`, the identity the panel states holds by
    // construction. It cannot disagree, so it cannot detect anything — and on 2026-08-27 every period
    // rendered Δ as exactly minus REALIZED with NET $0.00, which is the algebra of net==0, not data.
    //
    // The direction of the arrow is the whole assertion: Δ is now READ from the broker and NET is the
    // SUM. `equity - base_value` survives as an independent check, not as the source.
    expect(code).not.toMatch(/deltaUnrealized\s*\(/);
    expect(code).toMatch(/measuredUnrealizedDelta\s*\(/);
    expect(code).toMatch(/netFromComponents\s*\(\s*realized\s*,\s*measuredDelta\s*\)/);
    // AND the curve stays available as the FALLBACK, not as the primary. An em dash on 1W/1M/3M is
    // as useless as the $0.00 it replaced — Operator, 2026-08-27: "i have no stats ... cannot run like
    // this". The measured term wins where it exists; the curve fills in where it does not.
    expect(code).toMatch(/netMeasured\s*!==\s*null\s*\?\s*netMeasured\s*:\s*curveNet/);
    // And the reader is told which of the two they are looking at.
    expect(code).toMatch(/dUnrealizedIsDerived/);
  });

  it("renders the delta as a cell value, and the standing level only as its sub-line", () => {
    // `value={fmtUsd(unrealized)}` is the exact expression that shipped the bug. The standing figure is
    // still allowed on screen — as `sub`, where it is labelled "standing" and cannot be mistaken for the
    // window's own movement.
    expect(code).not.toMatch(/value=\{fmtUsd\(unrealized\)\}/);
    // FOLLOWS THE UNIT SWITCH (#586) WITHOUT WEAKENING. The cell now renders through `inUnit`, which
    // formats the SAME value in the selected unit — the assertion still names the variable, which is
    // the whole point of this guard: the DELTA is the cell value, never the standing level. Matching
    // `inUnit\(dUnrealized` keeps it discriminating; a looser `value=\{inUnit\(` would pass with the
    // standing figure substituted, which is exactly the bug #345 shipped.
    expect(code).toMatch(/value=\{inUnit\(dUnrealized, "dUnrealized"\)\}/);
    // And the standing level stays a SUB-LINE, in dollars: its denominator is cost basis, which this
    // component does not carry, and #586 forbids substituting a different base.
    // The sub-line carries the standing level IN DOLLARS, labelled; since #808 it may continue with
    // the lanes' Σ mark, and since #1077 a unit refusal may precede it (the visible "no 1M base to
    // take % against" on a phone, where a title never renders). What this pins is that the standing
    // figure is `fmtUsd(unrealized)` and is named "standing" — not where in the line it sits.
    expect(code).toMatch(/\$\{fmtUsd\(unrealized\)\} standing/);
    expect(code).toMatch(/sub=\{`\$\{unitRefusalShort\("dUnrealized"\)/);
    expect(code).toMatch(/lane marks Σ/);
  });

  it("labels the now-figures so a window selector does not imply they follow it", () => {
    // SECURED / DEPLOYED / CASH / LIQUIDATION are all correct as instantaneous values, and all four sat
    // unlabelled beside a period tab. Constant-under-every-window is right for them and wrong for the
    // cell above, and nothing on screen said which was which.
    for (const label of ["Secured · now", "Deployed · now", "Cash · now", "Liquidation · now"]) {
      expect(code).toContain(label);
    }
  });
});
