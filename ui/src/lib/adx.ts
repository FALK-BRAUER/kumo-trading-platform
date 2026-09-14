/**
 * Wilder's ADX / +DI / -DI (Average Directional Index), ported from kumo-trader's proven
 * `scanner/ichimoku.py:adx()` (pandas `ewm(alpha=1/period, adjust=False)`) — see #181. Formula verified
 * textbook-correct (Perplexity, 2026-07-31): TR/DM definitions match Wilder exactly; `adjust=False` EWM
 * with alpha=1/period is algebraically equivalent to Wilder's recursive smoothing. Period 9 (not the
 * textbook 14) is George's (Blue Cloud Trading) deliberate choice, confirmed in both the playbook doc and
 * kumo-trader's default.
 *
 * Arrays are indexed 1:1 with `bars` (`adx[0]` corresponds to `bars[0]`) — matching pandas' own indexing:
 * at bar 0 there's no previous bar, so TR = high-low only (no prevClose terms — pandas' `.max(axis=1)`
 * skips the NaN prevClose comparisons) and +DM/-DM = 0 (pandas: a NaN `up`/`down` compares False against
 * everything, `.where(False, 0.0)` → 0). Code review, #181: an earlier version started from bar 1 and
 * silently dropped bar 0 entirely, which is NOT what pandas does and produced observably different ADX
 * values on ordinary bars (codex's parity check flipped a real condition near the ADX=20 threshold).
 */
export interface OhlcBar {
  high: number;
  low: number;
  close: number;
}

export interface AdxSeries {
  adx: (number | null)[];
  plusDi: (number | null)[];
  minusDi: (number | null)[];
}

/** Wilder smoothing (`ewm(alpha=1/period, adjust=False)`): `y[0]=x[0]`, `y[t]=alpha*x[t]+(1-alpha)*y[t-1]`.
 *  A `null` input (pandas NaN — e.g. DX's 0/0 case) carries the prior smoothed value forward UNCHANGED,
 *  matching pandas' ewm NaN handling, rather than injecting a fabricated data point that would pull the
 *  series toward it. */
function wilderEma(values: (number | null)[], period: number): (number | null)[] {
  const alpha = 1 / period;
  const out: (number | null)[] = [];
  let prev: number | null = null;
  for (const v of values) {
    if (v == null) {
      out.push(prev);
      continue;
    }
    prev = prev == null ? v : alpha * v + (1 - alpha) * prev;
    out.push(prev);
  }
  return out;
}

export function computeAdx(bars: OhlcBar[], period = 9): AdxSeries {
  if (bars.length === 0) return { adx: [], plusDi: [], minusDi: [] };

  const tr: number[] = [];
  const plusDm: number[] = [];
  const minusDm: number[] = [];
  for (let i = 0; i < bars.length; i++) {
    const { high, low } = bars[i];
    if (i === 0) {
      tr.push(high - low);
      plusDm.push(0);
      minusDm.push(0);
      continue;
    }
    const prev = bars[i - 1];
    tr.push(Math.max(high - low, Math.abs(high - prev.close), Math.abs(low - prev.close)));
    const up = high - prev.high;
    const down = prev.low - low;
    plusDm.push(up > down && up > 0 ? up : 0);
    minusDm.push(down > up && down > 0 ? down : 0);
  }

  // `tr` has no null entries (TR is always defined, even at bar 0), so `atr` never actually carries a
  // null forward in practice — but wilderEma's type is general, so narrow explicitly per index.
  const atr = wilderEma(tr, period);
  // A zero-range ATR (every bar's high===low, no true range at all) makes +DI/-DI undefined — pandas
  // produces NaN there (division by zero), not a real 0. Matching that as null (not a fabricated 0) —
  // parity-verified against the real pandas scanner on 103 deterministic cases, this specific edge case
  // included (code review, #181).
  const diFrom = (dm: (number | null)[]) =>
    dm.map((v, i) => {
      const a = atr[i];
      return v == null || a == null || a === 0 ? null : (100 * v) / a;
    });
  const plusDi = diFrom(wilderEma(plusDm, period));
  const minusDi = diFrom(wilderEma(minusDm, period));
  // +DI/-DI both zero (or either undefined) makes DX's 0/0 undefined too, matching pandas' NaN
  // (`.replace(0, NaN)`) — null here, NOT 0, so `wilderEma` carries the prior ADX forward instead of a
  // fabricated zero pulling it down (code review, #181).
  const dx: (number | null)[] = plusDi.map((p, i) => {
    const m = minusDi[i];
    if (p == null || m == null) return null;
    const sum = p + m;
    return sum === 0 ? null : (100 * Math.abs(p - m)) / sum;
  });
  const adx = wilderEma(dx, period);

  return { adx, plusDi, minusDi };
}

/** `series[-1] > series[-1-lookback]` — false (never throws) if the series is too short, or either value
 *  is null/non-finite, to compare. */
export function isRising(series: (number | null)[], lookback = 3): boolean {
  const n = series.length;
  if (n <= lookback) return false;
  const last = series[n - 1];
  const prior = series[n - 1 - lookback];
  return last != null && prior != null && Number.isFinite(last) && Number.isFinite(prior) && last > prior;
}
