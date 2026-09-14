export interface TrendWindow {
  label: string;
  values: number[];
  /** False when the series does not reach back far enough to back the label. Render "—", not a number. */
  covered: boolean;
}

/** One bar reduced to what a trend window needs. `ts` is `ts_event`, in NANOSECONDS (Nautilus native). */
export interface TrendBar {
  ts: number;
  close: number;
}

export interface TrendSeries {
  m1: TrendBar[];
  h1: TrendBar[];
  d1: TrendBar[];
  w1: TrendBar[];
}

/** % change first vs last value in a window. Null if there's nothing to compare (or a zero baseline). */
export function pctChange(values: number[]): number | null {
  if (values.length < 2 || values[0] === 0) return null;
  return ((values[values.length - 1] - values[0]) / values[0]) * 100;
}

const NS_PER_DAY = 86_400_000_000_000;

/** Median gap between consecutive bars — the series' own idea of one period.
 *
 *  Median, not mean: weekends and holidays leave gaps several times the normal spacing, and a mean
 *  would let those stretch the coverage slack far beyond one bar. */
function medianSpacing(bars: TrendBar[]): number {
  if (bars.length < 2) return 0;
  const gaps: number[] = [];
  for (let i = 1; i < bars.length; i++) gaps.push(bars[i].ts - bars[i - 1].ts);
  gaps.sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)];
}

/** How much wall-clock history each label promises, and which granularity backs it.
 *
 * ONLY THE TWO GRANULARITIES EVERY VENUE SERVES DIRECTLY (#612). `m1` and `d1` are EXTERNAL — the
 * venue streams and backfills them. `h1` and `w1` are INTERNAL: Nautilus aggregates them locally from
 * the instrument's TRADE TICKS (`bar_spec.py` documents why, and it is correct for Alpaca).
 *
 * IBKR does not deliver those trade ticks. Measured on staging-ibkr 2026-08-27:
 *
 *     10189: Failed to request tick-by-tick data. No market data permissions for NYSE STK
 *      322 : Max number of tick-by-tick requests has been reached          (73 occurrences)
 *
 * So the aggregator's input never arrives and every `h1`/`w1` series is permanently empty there. The
 * portfolio screen showed it exactly: of six sparklines, ONLY 1M had data — and 1M was the only one
 * backed by `d1`. 1W (h1), 1Y and 5Y (w1) were structurally dead, not slow.
 *
 * A WEEK AND FIVE YEARS DO NOT NEED HOURS AND WEEKS. Operator: "the week and broader trend lines do not
 * need minute bars. day and hour would be great for that screen." Daily bars give 5 points for 1W,
 * ~252 for 1Y and ~1260 for 5Y — ample for a 24px sparkline, and honest about what it is drawing.
 *
 * VENUE-NEUTRAL BY CONSTRUCTION, not by branching. There is no `if (ibkr)` here and there must not
 * be: broker-specific code above the connector is #608, already open with four instances. Dropping
 * to the granularities both venues serve removes the dependency instead of special-casing it.
 */
const SPANS: { label: string; days: number; series: keyof TrendSeries }[] = [
  { label: "1H", days: 1 / 24, series: "m1" },
  { label: "1D", days: 1, series: "m1" },
  { label: "1W", days: 7, series: "d1" },
  { label: "1M", days: 30, series: "d1" },
  { label: "1Y", days: 365, series: "d1" },
  { label: "5Y", days: 5 * 365, series: "d1" },
];

/**
 * The 6 trend-strip windows, sliced by TIME and marked `covered` only when the series really reaches
 * back across the window.
 *
 * This used to slice by BAR COUNT off whatever had arrived — `1Y = w1.slice(-52)`, `5Y = w1.slice(-260)`
 * — and history streams in progressively, so the FIRST bar of each window kept moving and the baseline
 * moved with it. Two screenshots a minute apart, with identical last prices, showed JNJ 1Y at +6.96%
 * then +0.26%, and FIG 1Y at -3.33% then +21.22%. Nothing was recomputing the prices; the window itself
 * was sliding underneath them.
 *
 * The same slicing made 1H and 1D identical on every row — `slice(-60)` and `slice(-390)` return the
 * same array whenever fewer than 60 minute bars exist — and printed a confident "0.00%" for 1W/1M when
 * those series were empty, where a flat line reads as "did not move" rather than "no data".
 *
 * A window that cannot be backed is now `covered: false` so the strip can say it does not know, instead
 * of quoting a number computed over whatever happened to have loaded.
 */
export function buildTrendWindows(series: TrendSeries, nowNs?: number): TrendWindow[] {
  return SPANS.map(({ label, days, series: key }) => {
    const bars = series[key];
    if (bars.length < 2) {
      return { label, values: bars.map((b) => b.close), covered: false };
    }

    // Anchor on the NEWEST BAR, not wall-clock: outside market hours the last bar is legitimately old,
    // and anchoring on `now` would mark every window uncovered every evening and weekend.
    const end = nowNs ?? bars[bars.length - 1].ts;
    const start = end - days * NS_PER_DAY;
    const windowBars = bars.filter((b) => b.ts >= start);

    // Covered means the series reaches back across the window — with ONE BAR of slack, because a
    // series is only ever as granular as its bars. 261 weekly bars spanning 2021-08-09 to 2026-08-03
    // is five years of history by any reading, but it starts 1820 days before the last bar and a 5Y
    // window asks for 1825, so a strict test called it uncovered and rendered "—" on a symbol that
    // had the data. The slack is the series' own median spacing, so it scales with granularity
    // instead of being a magic number.
    const covered = bars[0].ts <= start + medianSpacing(bars) && windowBars.length >= 2;
    return { label, values: windowBars.map((b) => b.close), covered };
  });
}

/**
 * The % under a sparkline, at the shortest precision that still says something (#294).
 *
 * THE LABEL, NOT THE SPARKLINE, IS WHAT MAKES THE STRIP WIDE. Each cell is ~24px of chart, but
 * `toFixed(2)` on a five-year move renders "+254.65%" — eight characters, wider than the chart above
 * it — and six of those is what pushed the watchlist row past a phone's width.
 *
 * Precision falls as magnitude rises because that is where the information is. Hundredths of a percent
 * matter on a 1H move of +0.72%; on +254.65% they are noise, and the exact figure is one tap away on
 * the chart. This trades digits nobody reads for a row that fits, rather than dropping a timeframe
 * (#294: "Fit the screen width. Do not remove content.") or breaking the six onto two lines, which is
 * what the operator rejected: "You were to fit the width to my iPhone, but not by introducing a line break."
 *
 *     |v| < 10    2dp   "+0.72%"   6 chars
 *     |v| < 100   1dp   "+52.5%"   6 chars
 *     else        0dp   "+255%"    5 chars
 */
export function pctLabel(pct: number | null): string {
  if (pct == null || !Number.isFinite(pct)) return "—";
  const mag = Math.abs(pct);
  const digits = mag < 10 ? 2 : mag < 100 ? 1 : 0;
  return `${pct > 0 ? "+" : ""}${pct.toFixed(digits)}%`;
}
