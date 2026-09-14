import { describe, it, expect } from "vitest";
import { pctChange, pctLabel, buildTrendWindows } from "./trend";

describe("pctChange", () => {
  it("first vs last, sign-correct", () => {
    expect(pctChange([100, 105, 110])).toBeCloseTo(10);
    expect(pctChange([110, 105, 100])).toBeCloseTo(-9.0909, 3);
  });
  it("fewer than 2 values → null (nothing to compare)", () => {
    expect(pctChange([])).toBeNull();
    expect(pctChange([100])).toBeNull();
  });
  it("zero first value → null, never divide by zero", () => {
    expect(pctChange([0, 5])).toBeNull();
  });
  it("flat series → 0", () => {
    expect(pctChange([100, 100, 100])).toBe(0);
  });
});

describe("buildTrendWindows", () => {
  const DAY = 86_400_000_000_000;
  /** `n` bars ending at t=0, spaced `stepDays` apart, price rising by 1 each bar. */
  const series = (n: number, stepDays: number, base = 100) =>
    Array.from({ length: n }, (_, i) => ({ ts: -(n - 1 - i) * stepDays * DAY, close: base + i }));

  it("slices each window by TIME off the right series", () => {
    const windows = buildTrendWindows({
      m1: series(3000, 1 / 1440),
      h1: series(400, 1 / 24),
      d1: series(400, 1),
      w1: series(400, 7),
    });
    expect(windows.map((w) => w.label)).toEqual(["1H", "1D", "1W", "1M", "1Y", "5Y"]);
    // 1W/1M/1Y/5Y READ DAILY BARS NOW (#612). h1 and w1 are aggregated from trade ticks, which IBKR
    // refuses, so those series were permanently empty there and four of six sparklines were blank.
    //
    // The consequence is honest and worth pinning: 400 daily bars reach back 399 days, so a 5Y label
    // is NOT covered by this fixture. It used to be, off 400 weekly bars. Covering 5Y now takes 5
    // years of dailies — which the venue does serve, but this fixture deliberately does not.
    expect(windows.find((w) => w.label === "1M")!.covered).toBe(true);
    expect(windows.find((w) => w.label === "1Y")!.covered).toBe(true);
    expect(windows.find((w) => w.label === "5Y")!.covered).toBe(false);
    // A year of daily bars is a year of SESSIONS plus the boundary bar, not 52 points.
    expect(windows[4].values.length).toBeGreaterThanOrEqual(300);
    expect(windows[4].values.length).toBeLessThanOrEqual(400);
  });

  it("a window the series cannot reach back across is NOT covered", () => {
    // Six months of weekly bars cannot back a 1Y or 5Y label.
    const windows = buildTrendWindows({ m1: [], h1: [], d1: [], w1: series(26, 7) });
    expect(windows.find((w) => w.label === "1Y")!.covered).toBe(false);
    expect(windows.find((w) => w.label === "5Y")!.covered).toBe(false);
  });

  it("an empty series is uncovered, never a confident 0.00%", () => {
    const windows = buildTrendWindows({ m1: [], h1: [], d1: [], w1: [] });
    expect(windows.every((w) => !w.covered)).toBe(true);
    // pctChange of an empty window is null → the strip renders "—", not "0.00%", which would read as
    // "did not move" rather than "no data".
    expect(pctChange(windows[0].values)).toBeNull();
  });

  it("the reported % does NOT change as more history streams in", () => {
    // THE BUG: count-slicing took the last N bars, so the first bar — the baseline — moved every time
    // more history arrived. Two reads a minute apart, same last price, showed 1Y at +6.96% then +0.26%.
    // Fed as DAILY now (#612) — 1Y reads d1, not w1. The property under test is unchanged: the
    // baseline is fixed by TIME, so loading more history must not move the reported %.
    // Both slices must still SPAN the year, or the test proves only that an uncovered window is
    // uncovered. At daily spacing that means 800 bars vs the most recent 500 — same recent bars,
    // less history loaded, and both reach back past 365 days.
    const full = series(800, 1);
    const partial = full.slice(-500);

    const w = (bars: typeof full) => buildTrendWindows({ m1: [], h1: [], d1: bars, w1: [] })
      .find((x) => x.label === "1Y")!;

    expect(w(full).covered && w(partial).covered).toBe(true);
    expect(pctChange(w(partial).values)).toBeCloseTo(pctChange(w(full).values)!, 6);
  });

  it("1H and 1D are distinct windows, not the same array", () => {
    // slice(-60) and slice(-390) returned the SAME array whenever fewer than 60 minute bars existed,
    // which is why every row showed identical 1H and 1D percentages.
    const windows = buildTrendWindows({ m1: series(3000, 1 / 1440), h1: [], d1: [], w1: [] });
    expect(windows[0].values.length).toBeLessThan(windows[1].values.length);
    expect(pctChange(windows[0].values)).not.toBeCloseTo(pctChange(windows[1].values)!, 6);
  });

  it("anchors on the newest bar, so an out-of-hours series stays covered", () => {
    // Anchoring on wall-clock `now` would mark every window uncovered each evening and weekend.
    const stale = series(400, 1).map((b) => ({ ...b, ts: b.ts - 3 * DAY }));
    expect(buildTrendWindows({ m1: [], h1: [], d1: stale, w1: [] })
      .find((w) => w.label === "1Y")!.covered).toBe(true);
  });
});

describe("coverage slack", () => {
  const DAY = 86_400_000_000_000;
  const weekly = (n: number) =>
    Array.from({ length: n }, (_, i) => ({ ts: -(n - 1 - i) * 7 * DAY, close: 100 + i }));
  /** Daily bars, the granularity 1Y and 5Y actually read since #612. `n` bars span `n-1` days. */
  const daily = (n: number) =>
    Array.from({ length: n }, (_, i) => ({ ts: -(n - 1 - i) * DAY, close: 100 + i }));

  it("counts a series ONE BAR short of the window as covering it", () => {
    // THE PROPERTY, unchanged by #612; only the granularity moved. JNJ, live: 2021-08-09 -> 2026-08-03
    // spans 1820 of 1825 days — strictly short, and a no-slack test showed "—" on a symbol that
    // plainly had five years of history. The slack is ONE BAR, so it scales with the spacing: seven
    // days on weeklies, one day on dailies.
    //
    // 1Y and 5Y read DAILY now, so the case is expressed at that spacing: 1825 bars span 1824 days,
    // one short of 5*365, and must still count.
    const w = buildTrendWindows({ m1: [], h1: [], d1: daily(1825), w1: [] });
    expect(w.find((x) => x.label === "5Y")!.covered).toBe(true);
    expect(w.find((x) => x.label === "1Y")!.covered).toBe(true);
  });

  it("the slack is ONE BAR, not an arbitrary grace", () => {
    // A month short must NOT be waved through — otherwise "covered" stops meaning anything and the
    // strip quotes a 5Y % over four years of history.
    const w = buildTrendWindows({ m1: [], h1: [], d1: daily(1795), w1: [] });
    expect(w.find((x) => x.label === "5Y")!.covered).toBe(false);
  });

  it("still refuses a window the series misses by more than one bar", () => {
    // Six months of daily bars cannot back 5Y, slack or no slack.
    const w = buildTrendWindows({ m1: [], h1: [], d1: daily(182), w1: [] });
    expect(w.find((x) => x.label === "5Y")!.covered).toBe(false);
    expect(w.find((x) => x.label === "1Y")!.covered).toBe(false);
  });

  it("uses the MEDIAN gap, so a holiday hole does not widen the slack", () => {
    const bars = weekly(60);
    bars[0] = { ...bars[0], ts: bars[0].ts - 200 * DAY }; // one huge leading gap
    const w = buildTrendWindows({ m1: [], h1: [], d1: [], w1: bars });
    // ~59 weeks of real spacing + one outlier gap must not be inflated into 5Y coverage.
    expect(w.find((x) => x.label === "5Y")!.covered).toBe(false);
  });
});

describe("pctLabel — the strip's width is decided here (#294)", () => {
  it("spends digits where the information is", () => {
    expect(pctLabel(0.72)).toBe("+0.72%");
    expect(pctLabel(9.88)).toBe("+9.88%");
    expect(pctLabel(52.5)).toBe("+52.5%");
    expect(pctLabel(254.65)).toBe("+255%");
    expect(pctLabel(-74.52)).toBe("-74.5%");
  });

  it("never renders wider than 6 characters over the real payload's range", () => {
    // THE WHOLE POINT, AS A NUMBER. Six cells sized by their widest content is what has to fit a 204px
    // column. At 8px JetBrains Mono a character is ~4.8px, so 6 chars is ~29px and six cells plus gaps
    // land near 194px. A 7th character puts the row back over a phone's width.
    //
    // The old rendering is included as the control: `toFixed(2)` on a 5Y move is 8 characters, which is
    // what actually shipped the overflow.
    const real = [0.01, 0.72, 3.6, 5.57, 9.88, 12.06, 18.58, 34.78, 52.35, 51.33, -10.33, -26.31,
                  -48.84, -74.52, 254.65, -0.14];
    for (const v of real) {
      expect(pctLabel(v).length).toBeLessThanOrEqual(6);
    }
    expect(`+${(254.65).toFixed(2)}%`.length).toBe(8); // control: what it used to render
  });

  it("says nothing rather than zero when there is no number", () => {
    // A "0.00%" on an absent series reads as "did not move", which is a claim about the market.
    expect(pctLabel(null)).toBe("—");
    expect(pctLabel(Number.NaN)).toBe("—");
  });
})
