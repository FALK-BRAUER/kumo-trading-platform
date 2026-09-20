import { describe, it, expect } from "vitest";

import { dayNet, periodNet, periodNetCoverage, periodNetLabel } from "./periodNet";

/**
 * #336 — NET must be net FOR THE SELECTED PERIOD.
 *
 * The defect these pin, measured on a live paper account 2026-08-18 11:17 SGT with 8 positions held:
 * NET read $1,705.31 at 1D, 1W, 1M and 3M alike — identical to UNREALIZED — while REALIZED underneath it
 * read −$9.69 / $2,226.12 / $825.52 / $138.50. The hero number on Home did not move with the selector and
 * ignored every closed trade.
 *
 * The single most important property here is the LAST test: a missing curve must render "—", never the
 * standing unrealized. That fallback is the original bug.
 */

// The four windows as the broker published them that morning. Deliberately NOT monotonic: 1W > 1M > 3M is
// real (a longer window contains earlier losses), so a fixture that ordered them neatly would let a
// max()/sort() implementation pass while being wrong.
const FRAME = {
  curves: {
    "1D": { pnl: 583.07 },
    "1W": { pnl: 2923.12 },
    "1M": { pnl: 825.52 },
    "3M": { pnl: 116.4 },
    all: { pnl: 699.35 },
  },
};

describe("NET per period (#336)", () => {
  it("returns a DIFFERENT number for each period — the whole point", () => {
    // Assert the fixture can express the bug FIRST. If every window carried the same pnl, a hard-coded
    // implementation would pass this suite and we would have learned nothing.
    const values = ["1D", "1W", "1M", "3M"].map((p) => periodNet(FRAME, p));
    expect(new Set(values).size).toBe(4);

    expect(periodNet(FRAME, "1D")).toBe(583.07);
    expect(periodNet(FRAME, "1W")).toBe(2923.12);
    expect(periodNet(FRAME, "1M")).toBe(825.52);
    expect(periodNet(FRAME, "3M")).toBe(116.4);
  });

  it("does not assume the longer window is the larger number", () => {
    // 1W ($2,923.12) exceeds 3M ($116.40) because the quarter contains earlier losses the week does not.
    // Pinned because it looks like a bug and is not — anyone "fixing" it would reintroduce a real one.
    expect(periodNet(FRAME, "1W")).toBeGreaterThan(periodNet(FRAME, "3M")!);
  });

  it("reads the BROKER's pnl rather than deriving it from the curve endpoints", () => {
    // A curve whose endpoints disagree with the broker's own figure: last − base = 400, broker says 250.
    // The broker wins. Deriving locally would build a second ledger beside the statement (#242/#233).
    const frame = {
      curves: { "1W": { pnl: 250, base_value: 100_000, points: [{ equity: 100_400 }] } },
    };
    expect(periodNet(frame, "1W")).toBe(250);
  });

  it("a period the broker has not published is UNKNOWN, not zero", () => {
    // Zero asserts "this window did nothing", which is a different and much worse claim than "no data".
    expect(periodNet(FRAME, "5Y")).toBeNull();
    expect(periodNet({ curves: {} }, "1W")).toBeNull();
    expect(periodNet({}, "1W")).toBeNull();
    expect(periodNet(undefined, "1W")).toBeNull();
    expect(periodNet({ curves: { "1W": null } }, "1W")).toBeNull();
    expect(periodNet({ curves: { "1W": {} } }, "1W")).toBeNull();
    expect(periodNet({ curves: { "1W": { pnl: null } } }, "1W")).toBeNull();
  });

  it("a non-finite pnl is unknown, so the hero slot cannot render $NaN", () => {
    // Some encoders round-trip NaN/Infinity as real numbers. "$NaN" in the hero reads as a broken tile
    // rather than as missing data, and sends the reader hunting for a rendering fault that isn't there.
    expect(periodNet({ curves: { "1W": { pnl: NaN } } }, "1W")).toBeNull();
    expect(periodNet({ curves: { "1W": { pnl: Infinity } } }, "1W")).toBeNull();
  });

  it("zero is a real answer and must survive", () => {
    // A flat window genuinely nets $0.00. A truthiness check (`pnl ? pnl : null`) would swallow it and
    // show "—" for a period that is simply flat — the classic falsy-zero bug.
    expect(periodNet({ curves: { "1W": { pnl: 0 } } }, "1W")).toBe(0);
  });

  it("a negative net survives — losses are not missing data", () => {
    expect(periodNet({ curves: { "1D": { pnl: -9.69 } } }, "1D")).toBe(-9.69);
  });

  it("NEVER substitutes unrealized when the period is unknown — this was the bug", () => {
    // The regression in one line: NET was Σ(realized + unrealized), so it collapsed to the standing
    // unrealized and froze across every window. Nothing in this module may reach for unrealized: it does
    // not receive it, and it returns null instead of guessing. Pinned so no future "sensible fallback"
    // quietly restores the frozen hero number.
    expect(periodNet({ curves: {} }, "1D")).toBeNull();
  });

  it("names the figure a DELTA, because it is one (#654)", () => {
    // Operator: "net was supposed to be called delta net". The headline is realized + change in
    // unrealized over the window — a CHANGE — and it sits directly above CASH · NOW and
    // LIQUIDATION · NOW, which are levels. The one label that is a delta must not dress like the
    // two that are balances; Δ UNREALIZED is already honest the same way.
    expect(periodNetLabel("1W")).toBe("Delta net · 1W");
    expect(periodNetLabel("all")).toBe("Delta net · account");
  });

  /**
   * 1D has no curve. Measured 2026-08-18: the engine publishes 1W/1M/3M/all and nothing for 1D, so the
   * DEFAULT Home tab would have rendered the hero as "-". These pin the broker-sourced fallback.
   */
  describe("1D falls back to the broker's own day P&L", () => {
    // The live account that morning.
    const ACCOUNT = { equity: 100_658.95, last_equity: 100_116.28 };

    it("uses equity - last_equity when no 1D curve exists", () => {
      const noOneDay = { curves: { "1W": { pnl: 2922.86 } } };
      expect(periodNet(noOneDay, "1D", ACCOUNT)).toBeCloseTo(542.67, 2);
    });

    it("prefers a published 1D BASE over last_equity — but never a published 1D pnl (#343)", () => {
      // REWRITTEN for #343. This used to assert that a published `pnl` won over the fallback, on the
      // reasoning that the engine's own figure is authoritative. It is not: the broker measures `pnl` to
      // the last point of its portfolio history, so preferring it is precisely how 1W/1M/3M came to
      // exclude today. What is authoritative about a curve is its BASE — where the window starts. The
      // end is always now.
      const withBase = { curves: { "1D": { base_value: 100_000, pnl: 111.11 } } };
      expect(periodNet(withBase, "1D", ACCOUNT)).toBeCloseTo(ACCOUNT.equity - 100_000, 2);
      expect(periodNet(withBase, "1D", ACCOUNT)).not.toBe(111.11);

      // With no base published, 1D still anchors on the broker's previous close.
      const pnlOnly = { curves: { "1D": { pnl: 111.11 } } };
      expect(periodNet(pnlOnly, "1D", ACCOUNT)).toBeCloseTo(542.67, 2);
    });

    it("does NOT apply the fallback to any other window", () => {
      // 1W has no broker figure outside the curve plane, so a missing 1W curve is genuinely unknown.
      // Without this, a missing 1W would silently render the DAY number under a 1W label.
      expect(periodNet({ curves: {} }, "1W", ACCOUNT)).toBeNull();
      expect(periodNet({ curves: {} }, "3M", ACCOUNT)).toBeNull();
    });

    it("is unknown, not zero, when the broker account is absent or partial", () => {
      expect(dayNet(undefined)).toBeNull();
      expect(dayNet({})).toBeNull();
      expect(dayNet({ equity: 100_000 })).toBeNull();
      expect(dayNet({ last_equity: 100_000 })).toBeNull();
      expect(dayNet({ equity: NaN, last_equity: 1 })).toBeNull();
      expect(periodNet({ curves: {} }, "1D", undefined)).toBeNull();
    });

    it("a flat day is 0.00, not unknown", () => {
      expect(dayNet({ equity: 100_000, last_equity: 100_000 })).toBe(0);
    });

    it("a down day is negative", () => {
      expect(dayNet({ equity: 99_000, last_equity: 100_000 })).toBe(-1000);
    });
  });
});

/**
 * #343 — EVERY WINDOW MUST END AT NOW.
 *
 * Measured on a live instance paper account 2026-08-18 12:40 UTC (08:40 ET, premarket), 8 positions held.
 * Pulled straight off the `ui:state:equity_curve` plane, so these are Alpaca's own published numbers:
 *
 *   period  base_value   curve's LAST point                       pnl shown
 *   1W       97,755.71   Mon 17 Aug 15:30 ET, eq 100,678.57       +2,922.86
 *   1M       99,291.36   Mon 17 Aug close,    eq 100,847.61       +1,556.25
 *   3M/all  100,000.00   Mon 17 Aug close,    eq 100,847.61         +847.61
 *   1D      (last_equity 100,847.61)          eq 100,232.02 LIVE     -615.59
 *
 * Only 1D read live equity. Every other window took Alpaca's `profit_loss`, which is measured to the
 * LAST CLOSE — so 1W did not contain 1D. The account was funded with exactly $100,000 and stood at
 * $100,232.02, and the tile reported lifetime P&L as +$847.61: overstated by $615.59, which is precisely
 * the loss the day had taken. Operator: "net seems not unrel + rel".
 *
 * The fix is not a new calculation. Alpaca's own identity is `pnl == equity[-1] - base_value`
 * (`exec_client.py::_report_equity_curve`); we evaluate it at NOW rather than at `equity[-1]`. Both
 * operands remain the broker's own fields, so this stays inside the "broker is the anchor" rule.
 */
describe("every window ends at NOW (#343)", () => {
  // The live plane that morning. `pnl` is kept deliberately: it is what the old code read, so a fixture
  // without it could not tell a fixed implementation from one that simply lost the field.
  const LIVE = {
    curves: {
      "1W": { base_value: 97_755.71, pnl: 2922.86 },
      "1M": { base_value: 99_291.36, pnl: 1556.25 },
      "3M": { base_value: 100_000.0, pnl: 847.61 },
      all: { base_value: 100_000.0, pnl: 847.61 },
    },
  };
  const ACCOUNT = { equity: 100_232.02, last_equity: 100_847.61 };

  it("the fixture can express the bug: broker pnl and live equity genuinely disagree", () => {
    // Assert the fixture's own property before asserting the invariance. A frame whose curve already
    // ended at live equity would let the OLD implementation pass every test below, and we would have
    // pinned nothing. (CLAUDE.md: a test that cannot fail carries no information.)
    for (const p of ["1W", "1M", "3M", "all"]) {
      const c = LIVE.curves[p as keyof typeof LIVE.curves];
      expect(ACCOUNT.equity - c.base_value).not.toBeCloseTo(c.pnl, 2);
    }
  });

  it("all-time net is equity minus the funded base — the number that was most visibly wrong", () => {
    // Funded at exactly $100,000, standing at $100,232.02. Lifetime P&L is $232.02 and cannot be
    // $847.61, whatever the broker's to-last-close figure says.
    expect(periodNet(LIVE, "all", ACCOUNT)).toBeCloseTo(232.02, 2);
    expect(periodNet(LIVE, "3M", ACCOUNT)).toBeCloseTo(232.02, 2);
  });

  it("1W and 1M are measured to live equity, not to the last close", () => {
    expect(periodNet(LIVE, "1W", ACCOUNT)).toBeCloseTo(2476.31, 2);
    expect(periodNet(LIVE, "1M", ACCOUNT)).toBeCloseTo(940.66, 2);
  });

  it("THE WINDOWS COMPOSE: a longer window contains the day", () => {
    // The defect in one assertion. 1D was -615.59 while 1W claimed +2,922.86 — a week that did not
    // contain the day inside it. Every window now shares one endpoint, so widening the window can only
    // move the number by what the wider window adds.
    const day = periodNet(LIVE, "1D", ACCOUNT)!;
    const week = periodNet(LIVE, "1W", ACCOUNT)!;
    expect(day).toBeCloseTo(-615.59, 2);
    // The week equals its own base-to-now move, and the day is the tail of it: week - day is the move
    // from the week's base up to the PRIOR CLOSE, which must be what the broker's own base-to-prior-close
    // arithmetic says (last_equity - base_value).
    expect(week - day).toBeCloseTo(ACCOUNT.last_equity - LIVE.curves["1W"].base_value, 2);
  });

  it("ONE moving part: a change in live equity moves EVERY window by the same amount", () => {
    // The class-level property, not the instance. Any window frozen to a stale curve endpoint fails
    // here — which is what 1W/1M/3M/all all were, while 1D alone tracked. This is the assertion that
    // would have caught the original defect AND catches the next window that forgets to track.
    const before = ["1D", "1W", "1M", "3M", "all"].map((p) => periodNet(LIVE, p, ACCOUNT)!);
    const moved = { ...ACCOUNT, equity: ACCOUNT.equity + 100 };
    const after = ["1D", "1W", "1M", "3M", "all"].map((p) => periodNet(LIVE, p, moved)!);
    after.forEach((v, i) => expect(v - before[i]).toBeCloseTo(100, 6));
  });

  it("falls back to the broker's to-last-close pnl when there is no live equity", () => {
    // A node with no broker account (synthetic/non-Alpaca) still has a curve. A stale figure beats a
    // blank hero there, and `headlineEquity` next door already makes exactly this trade.
    expect(periodNet(LIVE, "1W", undefined)).toBe(2922.86);
    expect(periodNet(LIVE, "1W", {})).toBe(2922.86);
  });

  it("is unknown when neither live equity nor a base is available", () => {
    expect(periodNet({ curves: { "1W": { base_value: 97_755.71 } } }, "1W", undefined)).toBeNull();
    expect(periodNet({ curves: { "1W": { pnl: NaN, base_value: NaN } } }, "1W", ACCOUNT)).toBeNull();
  });

  it("a zero base_value is treated as missing, not as a $100k gain", () => {
    // `exec_client` writes `float(raw.get("base_value") or 0.0)`, so an absent base arrives as 0.0
    // rather than as null. Subtracting that from live equity would report the ENTIRE account balance as
    // the window's profit — the single most dangerous way this can fail.
    expect(periodNet({ curves: { "1W": { base_value: 0, pnl: 2922.86 } } }, "1W", ACCOUNT)).toBe(2922.86);
  });
});

describe("window coverage (#653)", () => {
  it("a clamped window says so instead of dressing as a full one", () => {
    // Staging, 2026-08-28: NET·3M == NET·1M to the cent on a ~2-week account — both bases clamped
    // to inception, nothing on screen saying it. The engine now stamps coverage on each curve; the
    // tile turns covered:false into a sublabel, never a silent full-window claim.
    const frame = {
      curves: {
        "3M": { base_value: 100_000, covered: false, covers_days: 14 },
        "1W": { base_value: 100_100, covered: true, covers_days: 7 },
      },
    };
    expect(periodNetCoverage(frame, "3M")).toEqual({ covered: false, coversDays: 14 });
    expect(periodNetCoverage(frame, "1W")).toEqual({ covered: true, coversDays: 7 });
  });

  it("a frame predating the field is UNKNOWN, not covered", () => {
    // Absence must not be readable as a full window — an old engine's frame simply has no opinion.
    expect(periodNetCoverage({ curves: { "3M": { base_value: 1 } } }, "3M")).toBeNull();
    expect(periodNetCoverage({ curves: {} }, "3M")).toBeNull();
  });
});
