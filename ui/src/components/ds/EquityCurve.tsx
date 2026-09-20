/**
 * EquityCurve (#243) — the account's equity over a period, as an area chart.
 *
 * Plain SVG on the design tokens rather than `lightweight-charts` (already a dependency, used by
 * `ChartTile`). A candlestick engine is the wrong tool for a single monotone series with no crosshair,
 * no volume pane and no indicators: it costs a chunk of JS for features this cannot use, and it draws
 * with its own colours instead of ours. This is the `Sparkline` idiom scaled up.
 *
 * Deliberately NOT time-scaled on the x-axis. Points are evenly spaced by index, so a weekend or a
 * holiday takes no horizontal room. A real time axis would render market closures as flat plateaus —
 * three days of "nothing happened" that look like three days of a dead account.
 */
import { sessionRuns } from "./sessionRuns";

export interface EquityPoint {
  /** Epoch SECONDS, as Alpaca sends them. */
  t: number;
  equity: number;
  /** Cumulative P&L for the period at this point, from the broker. */
  pnl: number;
}

const PAD_TOP = 8;
const PAD_BOTTOM = 18;
const PAD_RIGHT = 46;

export function EquityCurve({
  points,
  width = 640,
  height = 160,
  className = "",
}: {
  points: EquityPoint[];
  width?: number;
  height?: number;
  className?: string;
}) {
  if (points.length < 2) {
    return (
      <div
        className={`flex items-center justify-center font-mono text-[11px] text-t3 ${className}`}
        style={{ height }}
      >
        not enough history yet
      </div>
    );
  }

  const values = points.map((p) => p.equity);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  // A dead-flat series has no range. Widening it SYMMETRICALLY puts the line mid-box; the earlier
  // `max - min || …` only avoided the div-by-zero and left every point on the plot FLOOR, with the
  // three grid labels collapsed on top of each other. (codex review, Medium.)
  const flat = rawMax - rawMin < Number.EPSILON;
  const pad = flat ? Math.abs(rawMax) * 0.005 || 1 : 0;
  const min = rawMin - pad;
  const max = rawMax + pad;
  const span = max - min;
  const plotW = width - PAD_RIGHT;
  const plotH = height - PAD_TOP - PAD_BOTTOM;

  const x = (i: number) => (i / (points.length - 1)) * plotW;
  const y = (v: number) => PAD_TOP + plotH - ((v - min) / span) * plotH;

  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");

  // EXTENDED HOURS ARE DRAWN DIFFERENTLY (2026-08-25). Once #536 gave 1D a real 1Min series
  // WITH pre/post market, the shape changed: a smooth low-amplitude stretch, a sharp discontinuity,
  // then dense volatility. That discontinuity is the 09:30 open — thin pre-market marks giving way to
  // a fully quoted book — and undifferentiated it reads as a crash at an arbitrary time of day.
  //
  // It is not only cosmetic. Extended-hours equity is marked from THIN quotes, so the same position
  // is priced with far less information. One colour asserts the two are the same kind of measurement.
  //
  // Index-based x() means the runs must be located by INDEX, not recomputed from time — the axis is
  // ordinal here, so a run's screen position is where its points sit in the array.
  let cursor = 0;
  const segments = sessionRuns(points).map((run) => {
    // Runs overlap by one point (see sessionRuns), so a run's first point is the previous run's last.
    const start = cursor === 0 ? 0 : cursor - 1;
    cursor = start + run.points.length;
    const d = run.points
      .map((pt, k) => `${k === 0 ? "M" : "L"}${x(start + k).toFixed(1)},${y(pt.equity).toFixed(1)}`)
      .join(" ");
    return { session: run.session, d };
  });
  const extendedOnly = segments.every((sg) => sg.session !== "regular");
  const area = `${line} L${plotW.toFixed(1)},${(PAD_TOP + plotH).toFixed(1)} L0,${(PAD_TOP + plotH).toFixed(1)} Z`;

  // Direction over the WHOLE window — the same first-vs-last rule Sparkline uses, so a curve and a
  // sparkline never disagree about whether a thing is up.
  const up = values[values.length - 1] >= values[0];
  const stroke = up ? "var(--color-status-bull)" : "var(--color-status-bear)";
  const gradId = `eq-${up ? "up" : "down"}`;

  const fmt = (v: number) =>
    v >= 1000 ? `${(v / 1000).toFixed(1)}K` : v.toFixed(0);

  // Grid levels, as a FRACTION of plot height — so the labels can be positioned in CSS (which does not
  // stretch) while the path stays inside the non-uniformly scaled SVG.
  const levels = [max, (max + min) / 2, min].map((v) => ({
    v,
    pct: ((y(v) / height) * 100).toFixed(2),
  }));

  return (
    <div className={`relative ${className}`} style={{ height }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        role="img"
        aria-label={`Account equity, ${fmt(values[0])} to ${fmt(values[values.length - 1])}, ${
          up ? "up" : "down"
        } over the period`}
        // `none` lets the path fill any width. It would ALSO stretch glyphs, which is why no <text>
        // lives in here — the axis labels are HTML below. (codex review, Medium.)
        preserveAspectRatio="none"
        style={{ paddingRight: PAD_RIGHT }}
      >
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.18" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>
        {levels.map((l, i) => (
          <line
            key={i}
            x1="0"
            y1={y(l.v)}
            x2={plotW}
            y2={y(l.v)}
            stroke="var(--color-ds-line)"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
        ))}
        <path d={area} fill={`url(#${gradId})`} />
        {segments.map((sg, i) => (
          <path
            key={i}
            d={sg.d}
            fill="none"
            // Amber for pre/post. NOT bull/bear: the point is WHICH SESSION priced this, not whether
            // it went up — a green pre-market stretch and a green regular one are the same claim
            // about direction and a different claim about how well the book was marked.
            //
            // When the whole window is extended hours (before the open) the line stays on the normal
            // colour: everything amber says nothing, and an indicator that is always on is off.
            stroke={sg.session === "regular" || extendedOnly ? stroke : "var(--color-status-warn)"}
            strokeWidth="1.75"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>

      {/* Axis labels in HTML, immune to the SVG's non-uniform scale. */}
      {levels.map((l, i) => (
        <span
          key={i}
          className="pointer-events-none absolute right-0 -translate-y-1/2 font-mono text-[9px] text-t3"
          style={{ top: `${l.pct}%` }}
          aria-hidden
        >
          {fmt(l.v)}
        </span>
      ))}
    </div>
  );
}
