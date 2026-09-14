import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { deriveSessionCurve, sessionPoints, type DerivedCurve } from "./sessionCurve";

/** Epoch SECONDS for an ET wall-clock time, the way Alpaca sends them. */
const et = (day: number, h: number, m = 0) =>
  Date.parse(`2026-08-${String(day).padStart(2, "0")}T${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:00-04:00`) / 1000;
const NOW = Date.parse("2026-08-20T11:00:00-04:00");

const pt = (t: number, equity: number) => ({ t, equity, pnl: 0 });

describe("the 1D curve is derived, because none is ever fetched (#345)", () => {
  it("keeps only the current ET session", () => {
    // The 1W series is hourly and spans days; today is a suffix of it.
    const week = [pt(et(19, 10), 100), pt(et(19, 15), 101), pt(et(20, 10), 102), pt(et(20, 11), 103)];
    expect(sessionPoints(week, NOW).map((p) => p.equity)).toEqual([102, 103]);
  });

  it("is a SESSION, not a rolling 24 hours", () => {
    // At 11:00 ET the honest answer is this morning, not yesterday afternoon dragged in behind it —
    // which a `now - 24h` window would include and which would disagree with `last_equity`.
    const week = [pt(et(19, 15), 999), pt(et(20, 10), 102), pt(et(20, 11), 103)];
    expect(sessionPoints(week, NOW).some((p) => p.equity === 999)).toBe(false);
  });

  it("measures P&L from where the DAY started, not where the week did", () => {
    const c = deriveSessionCurve(
      { points: [pt(et(19, 10), 90), pt(et(20, 10), 100), pt(et(20, 11), 104)], base_value: 90, pnl: 14, timeframe: "1H" },
      NOW,
    )!;
    expect(c.base_value).toBe(100); // the session's first point, not the week's
    expect(c.pnl).toBe(4);
  });

  it("returns null rather than drawing a flat line when the session has not started", () => {
    // A single point is a dot, not a curve, and a flat line at the current level would ASSERT the day
    // was unchanged. The caller must keep saying "not enough history yet" — which is true then.
    expect(deriveSessionCurve({ points: [pt(et(20, 10), 100)], base_value: 100, pnl: 0, timeframe: "1H" }, NOW)).toBeNull();
    expect(deriveSessionCurve({ points: [], base_value: 0, pnl: 0, timeframe: "1H" }, NOW)).toBeNull();
    expect(deriveSessionCurve(undefined, NOW)).toBeNull();
  });

  it("ignores points with an unusable timestamp instead of placing them at the epoch", () => {
    const week = [pt(NaN, 50), pt(et(20, 10), 102), pt(et(20, 11), 103)];
    expect(sessionPoints(week, NOW).map((p) => p.equity)).toEqual([102, 103]);
  });
});

describe("the tile actually uses it", () => {
  it("reads the REAL 1D curve first and derives one only as a fallback", () => {
    // The seam, not the unit. `deriveSessionCurve` working proves nothing about whether the 1D tab calls
    // it — and the 1D tab silently rendering "not enough history yet" is the entire defect. Asserted on
    // source because nothing here can render a tile.
    //
    // THIS TEST USED TO BE CALLED "derives 1D rather than reading a curve the backend never sends",
    // and that premise expired with #536: `_EQUITY_PERIODS` now fetches ("1D", "1Min", extended), so
    // the backend DOES send one. The assertion below was already the right one — `curves["1D"] ??`
    // pins the ORDER — but a test whose name asserts something false is the "comment that reads as
    // safety and is wrong" shape, and it survives review by sounding settled.
    const src = readFileSync(join(import.meta.dirname, "EquityTile.tsx"), "utf8");
    const code = src.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
    expect(code).toMatch(/deriveSessionCurve\(/);
    expect(code).toMatch(/curves\["1D"\]\s*\?\?/);
  });
})

// -------------------------------------------------------------------------------------------
// #536: a REAL 1D series now exists. The derived one becomes a fallback, and the order matters.
// -------------------------------------------------------------------------------------------

describe("1D curve precedence", () => {
  // The expression in EquityTile.tsx:51, restated. Not imported, because the tile is a component and
  // the property under test is one line of selection logic — but the two must not drift, which is
  // what the source assertion below is for.
  const pick = (curves: Record<string, DerivedCurve | undefined>, nowMs: number) =>
    curves["1D"] ?? deriveSessionCurve(curves["1W"], nowMs);

  const NOW = Date.parse("2026-08-25T14:00:00Z"); // 10:00 ET, mid-session
  const hourly = (n: number): DerivedCurve => ({
    points: Array.from({ length: n }, (_, i) => ({
      t: Math.floor(Date.parse("2026-08-25T13:30:00Z") / 1000) + i * 3600,
      equity: 103_600 + i * 100,
      pnl: 0,
    })),
    base_value: 103_600,
    pnl: 0,
    timeframe: "1H",
  });

  it("prefers the REAL 1D series over the derived slice", () => {
    // THE WHOLE POINT OF #536. Before it, `curves["1D"]` was always undefined and the derived
    // hourly slice — two points ten minutes into a session — was ALL there was to draw.
    const real: DerivedCurve = {
      points: [
        { t: 1_787_000_000, equity: 103_600, pnl: 0 },
        { t: 1_787_000_060, equity: 103_650, pnl: 0 },
        { t: 1_787_000_120, equity: 103_700, pnl: 0 },
      ],
      base_value: 103_600,
      pnl: 100,
      timeframe: "1Min",
    };
    const chosen = pick({ "1D": real, "1W": hourly(4) }, NOW);

    expect(chosen?.timeframe).toBe("1Min");
    expect(chosen?.points).toHaveLength(3);
  });

  it("falls back to the derived slice when the real series has not arrived", () => {
    // The equity plane refreshes every 120s, so there IS a window after boot where 1D is absent.
    // Losing the fallback would blank the default tab for up to two minutes on every restart.
    const chosen = pick({ "1W": hourly(4) }, NOW);

    expect(chosen).not.toBeNull();
    expect(chosen?.timeframe).toBe("1H");
  });

  it("draws nothing rather than a dot when neither is available", () => {
    // `deriveSessionCurve` returns null below two points — "one point is not a curve; it is a dot".
    // A flat line at the current level would assert the day was unchanged, which is the failure
    // this file's header names.
    expect(pick({}, NOW)).toBeNull();
    expect(pick({ "1W": hourly(1) }, NOW)).toBeNull();
  });
});

