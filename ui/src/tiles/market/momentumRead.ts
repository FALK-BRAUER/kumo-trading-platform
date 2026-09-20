/**
 * The momentum map (#351, second panel) — drift against travel, and how little of it is real.
 *
 * A rotation graph places each axis by two numbers: WHERE the ratio stands over the window (`est`,
 * the drift) and WHICH WAY that standing is moving (`move`, the travel versus the previous window).
 * The sign pair names a quadrant, and the quadrant is the whole point of the picture:
 *
 *        travel +           │  Improving  │   Leading    │   drift < 0 and rising, drift > 0 and rising
 *        travel −           │   Lagging   │  Weakening   │   drift < 0 and falling, drift > 0 and falling
 *                            drift −        drift +
 *
 * WHY THIS MODULE EXISTS SEPARATELY FROM THE ROW PANEL. `rotationRead.ts` scales each rail by ITS OWN
 * interval, which is right for a row — every rail is read alone and an axis moving in tenths must not
 * render as a dead line next to one moving in whole percents. A scatter is the opposite case: every
 * dot shares one pair of axes, so a per-point scale would place two different numbers at the same
 * pixel and the picture would be a lie. The scale here is therefore computed ACROSS the set.
 *
 * THE UNCOMFORTABLE PART, AND THE REASON THE INTERVAL IS PLOTTED. Measured against the live payload on
 * 2026-08-20: of 125 axis-windows, FIVE are separable from zero, all five at 1D. At 1W, 1M, 3M and 1Y
 * the count is zero out of twenty-five. A conventional rotation graph drawn on this data would place
 * twenty-five dots in four confidently-labelled quadrants when not one of them has a drift
 * distinguishable from no drift at all — the exact overstatement the row panel was built to refuse,
 * reintroduced in a prettier form.
 *
 * So the map plots the INTERVAL, not the point. Each axis is a horizontal bar spanning `lo`…`hi` with
 * a marker at `est`, and a bar that crosses the centre line is visibly claiming both quadrants at once
 * — which is exactly what the number is saying. An operator sees the honest picture without reading a
 * footnote, and on a day when something IS separable, that bar clears the line and stands out on its
 * own. The tile does not become more confident; the data does.
 */
import type { RotationWindow } from "@/lib/api/client";

export type Quadrant = "Leading" | "Improving" | "Weakening" | "Lagging";

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/**
 * The quadrant, or null when either coordinate is missing.
 *
 * Null rather than a default corner. A missing `move` means the previous window was never computed,
 * and placing that axis in "Lagging" because zero is not positive would invent a read from an absence
 * — the same rule NET follows for a window the broker has not published (#343).
 *
 * Exact zero resolves to the NEGATIVE side on both axes, so the strict comparisons here and the ones a
 * reader draws from the diagram above agree. It is a boundary that floating point makes practically
 * unreachable; it is pinned anyway so that whoever changes it has to mean it.
 */
export function quadrantOf(win: RotationWindow | null | undefined): Quadrant | null {
  if (!win || !finite(win.est) || !finite(win.move)) return null;
  const rising = win.move > 0;
  const ahead = win.est > 0;
  if (ahead) return rising ? "Leading" : "Weakening";
  return rising ? "Improving" : "Lagging";
}

/**
 * Is the drift separable from zero — does the 95% interval keep the estimate's sign?
 *
 * This is the fact that decides whether an axis's quadrant means anything, and it is derived from the
 * interval rather than read from the payload's own `sig` flag ON PURPOSE. Two derivations of one fact
 * are a detector: `test_sig_and_the_interval_agree` pins that the upstream flag and this arithmetic
 * never disagree on a real payload, and the day they do, one of them is broken and we find out here
 * instead of on the screen.
 */
export function separable(win: RotationWindow | null | undefined): boolean {
  if (!win || !finite(win.lo) || !finite(win.hi)) return false;
  return win.lo > 0 || win.hi < 0;
}

export interface MapScale {
  x: number;
  y: number;
}

/**
 * One scale for the whole scatter, taken from the widest thing that has to fit.
 *
 * `hi`/`lo` and not just `est`: the bars are the point of the picture, and a scale derived from the
 * markers alone would push every interval off the plot. The floor keeps a degenerate set (one axis,
 * all zeros) from dividing by nothing and painting the map at infinity.
 */
export function mapScale(wins: Array<RotationWindow | null | undefined>): MapScale {
  // THE 85TH PERCENTILE OF THE ESTIMATES, NOT THE MAX OF THE INTERVALS.
  //
  // This took `Math.max` over `est`, `lo` AND `hi` — so the single widest confidence interval across
  // all 25 axes set the x-scale, and every point estimate collapsed into the middle. Measured on the
  // live 1D payload: mean |x| was 0.079, i.e. the average marker sat 4% of the way from the centre to
  // the edge. The map rendered as a vertical line through the origin, which is not a map.
  //
  // Operator, looking at it: "Cannot see much more" — and he was right for a reason that had nothing to do
  // with the labels. There was no room to label because there was no SEPARATION.
  //
  // The rail already learned this. `railDomain` takes the 85th percentile "because per-row scaling
  // makes two rails with wildly different magnitudes look identical — the whole column becomes
  // unreadable as a column". Same failure, same fix, one panel over.
  //
  // WHAT THIS COSTS, STATED. Roughly 7 of 25 axes now clamp to the frame instead of 1, so an outlier's
  // exact position is no longer readable — it is pinned at the edge. That is the deliberate trade:
  // the map answers "which corner is filling up", a question about the SET, and a scale set by the
  // most extreme member answers it for nobody. The rail below carries each axis's precise interval.
  //
  // `lo`/`hi` are gone from the scale entirely. The interval belongs on the rail; using its ends to
  // scale the marker positions meant the uncertainty of one axis moved every other axis's dot.
  const ests: number[] = [];
  const moves: number[] = [];
  for (const w of wins) {
    if (!w) continue;
    if (finite(w.est)) ests.push(Math.abs(w.est as number));
    if (finite(w.move)) moves.push(Math.abs(w.move as number));
  }
  return { x: Math.max(percentile(ests, 0.85), 1e-9), y: Math.max(percentile(moves, 0.85), 1e-9) };
}

/** Linear-interpolated percentile of an unsorted list. Empty -> 0, which callers clamp away from zero. */
function percentile(values: number[], p: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const i = (sorted.length - 1) * p;
  const lo = Math.floor(i);
  const hi = Math.ceil(i);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
}

export interface MapPoint {
  /** Marker position, −1 … +1. */
  x: number;
  /** Travel, −1 … +1. */
  y: number;
  /** Interval ends in the same −1 … +1 space; null when the payload carries no interval. */
  lo: number | null;
  hi: number | null;
  quadrant: Quadrant;
  separable: boolean;
}

/**
 * Place one axis on the plot, or refuse to.
 *
 * Refusing is the same decision `driftLabel` makes: an axis with no interval is not drawn at a
 * confident-looking point, it is left off and counted as unplottable, because a marker with no bar
 * reads as the most certain thing on screen precisely when we know least about it.
 */
export function mapPoint(
  win: RotationWindow | null | undefined,
  scale: MapScale,
): MapPoint | null {
  const quadrant = quadrantOf(win);
  if (!win || quadrant === null) return null;
  const clamp = (v: number) => Math.max(-1, Math.min(1, v));
  const span = (v: number | null | undefined) => (finite(v) ? clamp(v / scale.x) : null);
  return {
    x: clamp((win.est as number) / scale.x),
    y: clamp((win.move as number) / scale.y),
    lo: span(win.lo),
    hi: span(win.hi),
    quadrant,
    separable: separable(win),
  };
}

/**
 * How much of this map is load-bearing: separable axes over plotted axes.
 *
 * Rendered as a headline rather than buried, because on most windows the honest answer is "none of
 * it" and an operator deciding whether to act on a rotation needs that before they read a single dot.
 */
export function separableCount(wins: Array<RotationWindow | null | undefined>): {
  separable: number;
  plotted: number;
} {
  let sep = 0;
  let plotted = 0;
  for (const w of wins) {
    if (quadrantOf(w) === null) continue;
    plotted += 1;
    if (separable(w)) sep += 1;
  }
  return { separable: sep, plotted };
}

/**
 * PLOT GEOMETRY LIVES HERE, NOT IN THE COMPONENT — because this repo can render nothing in a test.
 *
 * `vitest` runs on the node environment with no react-testing-library, so a coordinate computed inside
 * `MomentumMap.tsx` is a coordinate nothing can check. Both edge defects found in the browser on
 * 2026-08-20 were geometry: a clamped marker landed exactly on the frame and `overflow-hidden` shaved it
 * in half, and a label fixed to the right of its marker ran outside the clip box for anything in the
 * right half (GDX/GLD and XLRE/KRE both lost characters). Pure functions here make both facts assertable
 * without adding a render stack at all.
 */

/** Inset, in percent, so a clamped value lands INSIDE the frame rather than on its border. */
export const PLOT_INSET = { x0: 6, xSpan: 88, y0: 8, ySpan: 84 } as const;

/** Left-right: -1 … +1 onto the inset band. */
export function plotX(v: number): number {
  return PLOT_INSET.x0 + ((v + 1) / 2) * PLOT_INSET.xSpan;
}

/** Up-down, INVERTED — travel is positive upward and CSS `top` grows downward. */
export function plotY(v: number): number {
  return PLOT_INSET.y0 + ((1 - v) / 2) * PLOT_INSET.ySpan;
}

/**
 * Which side of its marker a label sits on: always the side facing the centre.
 *
 * A fixed side is what put labels outside the frame. The marker carries the position, so the label is
 * free to move inward and stay legible at every width.
 */
export function labelSide(x: number): "left" | "right" {
  return x > 0 ? "left" : "right";
}

// ==================================================================================================
// LABEL PLACEMENT (#388)
//
// `MomentumMap` placed every label at a fixed offset from its dot with NO collision handling at all.
// Axes cluster near the origin by construction — most rotations are small — so the labelled ones, which
// are chosen for being FURTHEST from the origin, are still frequently close to each other. Observed on
// a phone 2026-08-20: `GLD/SPY` printed over `XLV/SPY` over `GDX/GLD`, and `XLI/XLV` over `XLI/XLY`.
// Operator: "labels are missing. can't even click" — they were not missing, they were stacked.
//
// WHY THIS NEEDS THE BOX SIZE, and why that overturns the component's stated "no layout JS" rule.
// Percentage-positioned absolutes reflow for free, which is why the original chose them — but whether
// two labels collide is a question about PIXELS: an 8-character label is ~44px wide whatever percentage
// its dot sits at, so the same two dots collide on a phone and clear each other on a desktop. The rule
// that avoided measuring the box is exactly the rule that made this bug, so the map now measures once
// and hands the number to this pure function.
//
// GREEDY, IN PRIORITY ORDER, AND A LOSER IS DROPPED RATHER THAN DRAWN. #388: "A label that cannot be
// placed legibly should not be drawn — the dot's position is still information." Both sides are tried
// before giving up, because turning a label inward is free and usually enough.
// ==================================================================================================

/** Monospace advance at `text-[8px]`. Dropped from 9px on the operator's ask ("font can be smaller") — and it
 *  is not only cosmetic: an 8-character label goes from ~44px to ~38px, which is what lets more of them
 *  find a free slot. Narrower labels are the cheapest way to fit more of them. */
const CHAR_W = 4.82;
const LABEL_H = 10;
/** Gap between a dot and its label. */
const LABEL_GAP = 6;
/** Half-extent of a marker, as an obstacle. The dot is 8px plus a 1.5px ring; 6 gives it a little air.
 *
 *  THE DOTS WERE NEVER OBSTACLES, and that is what the operator was looking at: `GLD/SPY` printed straight
 *  through a marker, and two labels sat on markers at the bottom of the cluster. Collision was checked
 *  label-vs-label and label-vs-corner-caption only, so a label could not land on another LABEL and
 *  could land anywhere on the data. */
const DOT_R = 6;

interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

function overlaps(a: Rect, b: Rect): boolean {
  return a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
}

export type LabelSide = "left" | "right" | "above" | "below";

export interface Placement {
  pair: string;
  side: LabelSide;
}

export function placeLabels(
  candidates: Array<{ pair: string; x: number; y: number }>,
  box: { width: number; height: number },
  /** EVERY plotted point, not just the labelled ones. Markers are obstacles: a label through a dot is
   *  as unreadable as a label through another label, and it also hides data. */
  dots: Array<{ x: number; y: number }> = [],
): Placement[] {
  const { width, height } = box;
  if (!(width > 0) || !(height > 0)) return [];

  // Reserved corner captions. ~9 chars, plus the insets the map renders them at.
  const cw = 9 * CHAR_W;
  const taken: Rect[] = [
    { x0: 4, y0: 2, x1: 4 + cw, y1: 2 + LABEL_H },
    { x0: width - 4 - cw, y0: 2, x1: width - 4, y1: 2 + LABEL_H },
    { x0: 4, y0: height - 4 - LABEL_H, x1: 4 + cw, y1: height - 4 },
    { x0: width - 4 - cw, y0: height - 4 - LABEL_H, x1: width - 4, y1: height - 4 },
  ];

  // Every marker, reserved up front — including the ones that will never carry a label. A cluster of
  // unlabelled dots is exactly where a label must not be written.
  for (const d of dots) {
    const cx = (plotX(d.x) / 100) * width;
    const cy = (plotY(d.y) / 100) * height;
    taken.push({ x0: cx - DOT_R, y0: cy - DOT_R, x1: cx + DOT_R, y1: cy + DOT_R });
  }

  const out: Placement[] = [];
  for (const c of candidates) {
    const cx = (plotX(c.x) / 100) * width;
    const cy = (plotY(c.y) / 100) * height;
    const w = c.pair.length * CHAR_W;

    // FOUR SIDES, NOT TWO. Inward-horizontal first (the original rule, and the most readable), then
    // outward, then above/below. In a vertical cluster — which is what the live map is, most axes
    // sitting near the origin — the horizontal slots are all taken by neighbouring dots and the only
    // free space is above and below. Two candidate positions is why so few labels were drawn.
    const inward = labelSide(c.x);
    const order: LabelSide[] = [inward, inward === "left" ? "right" : "left", "above", "below"];

    for (const side of order) {
      let rect: Rect;
      if (side === "left") {
        rect = { x0: cx - LABEL_GAP - w, y0: cy - LABEL_H / 2, x1: cx - LABEL_GAP, y1: cy + LABEL_H / 2 };
      } else if (side === "right") {
        rect = { x0: cx + LABEL_GAP, y0: cy - LABEL_H / 2, x1: cx + LABEL_GAP + w, y1: cy + LABEL_H / 2 };
      } else if (side === "above") {
        rect = { x0: cx - w / 2, y0: cy - LABEL_GAP - LABEL_H, x1: cx + w / 2, y1: cy - LABEL_GAP };
      } else {
        rect = { x0: cx - w / 2, y0: cy + LABEL_GAP, x1: cx + w / 2, y1: cy + LABEL_GAP + LABEL_H };
      }
      // Off the frame is as unreadable as overlapping — in both axes now that labels can go vertical.
      if (rect.x0 < 0 || rect.x1 > width || rect.y0 < 0 || rect.y1 > height) continue;
      if (taken.some((t) => overlaps(rect, t))) continue;
      taken.push(rect);
      out.push({ pair: c.pair, side });
      break;
    }
  }
  return out;
}
