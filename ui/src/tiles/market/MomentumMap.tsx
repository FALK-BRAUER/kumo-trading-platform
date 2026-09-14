"use client";

/**
 * The momentum map (#351, second panel) — every rotation on one pair of axes.
 *
 * The row panel answers "what is this axis doing", one line at a time. It cannot answer "where is money
 * moving", because that question is about the SET: which corner is filling up, which axis is leaving the
 * one it was in.
 *
 * WHAT THE FIRST CUT GOT WRONG. It drew each axis as its 95% interval — a horizontal bar — reasoning
 * that since almost no axis is separable from zero, a dot-per-axis map would label twenty-five rotations
 * with quadrants none of them support. The reasoning is sound and the design was not: twenty-five grey
 * bars over a grey grid, labels only on the handful of separable axes (so at 1W, none at all). Operator, on
 * a phone: "hard to read … did the mock up really have the spread in the chart. color difficult. no
 * labels. no colors."
 *
 * It did not. The design reference (gist f7db5571) puts the interval on the RAIL, where a row is read
 * one at a time and a band is legible, and keeps the map to one coloured dot per axis with the strongest
 * movers named. The uncertainty is not hidden by that — it is stated once, in words, above the plot, and
 * carried per-axis by the rail below. Spelling it out twenty-five times inside a 224px box communicated
 * nothing except that the picture was unreadable.
 *
 * COLOUR IS NEVER ALONE. Every dot's quadrant is also its position, the corners are named in words, and
 * the rows below carry the same colour beside the same Read word.
 *
 * NO SVG. Percentage-positioned absolutes reflow with the column and survive every breakpoint for free.
 *
 * IT USED TO SAY "AND NO LAYOUT JS", AND THAT RULE CAUSED #388. Whether two labels collide is a
 * question about PIXELS — an 8-character label is ~44px wide whatever percentage its dot sits at, so
 * the same two dots collide on a phone and clear each other on a desktop. Refusing to measure the box
 * meant refusing to know, and the result was `GLD/SPY` printed over `XLV/SPY` over `GDX/GLD`. The map
 * now measures its own width once per resize and hands the number to `placeLabels`, which is pure and
 * carries all the geometry. Positioning is still percentages; only the collision test needs pixels.
 */
import { useEffect, useRef, useState } from "react";
import type { RotationAxis, RotationWindow } from "@/lib/api/client";
import { gateOf, isConfirmed } from "./rotationRead";
import {
  mapPoint,
  mapScale,
  plotX,
  placeLabels,
  type LabelSide,
  plotY,
  separableCount,
  type MapPoint,
} from "./momentumRead";

const px = (v: number) => `${plotX(v)}%`;
const py = (v: number) => `${plotY(v)}%`;

/** How many axes are OFFERED a name, in priority order. Not how many get one.
 *
 *  This used to be a quota of 8 (5 when short) because labels were drawn unconditionally and more than
 *  that turned into noise. `placeLabels` now refuses anything it cannot draw legibly, so the cap can be
 *  the whole set and the geometry decides — Operator: "can we add more? can we place them that they do not
 *  overlap too much?". Those are the same question: labels fit when they are allowed to move, and the
 *  cap was standing in for a placement rule that did not exist. */
const NAMED = 25;
const NAMED_NARROW = 14;

function Corner({ label, at }: { label: string; at: string }) {
  return (
    <span className={`pointer-events-none absolute ${at} font-mono text-[9px] uppercase tracking-wide text-t3`}>
      {label}
    </span>
  );
}

function Dot({
  axis,
  point,
  side,
  selected,
  onSelect,
}: {
  axis: RotationAxis;
  point: MapPoint;
  /** Where the label sits, or null when it could not be placed without colliding. */
  side: LabelSide | null;
  selected: boolean;
  onSelect: () => void;
}) {
  // The axis's own gate colour — the SAME family the rail below uses, so an axis is the same colour
  // wherever it appears. Quadrant is carried by position and by the named corners, not by hue.
  const gate = gateOf(axis.verdict);
  const tone =
    gate === "bull" ? "text-status-bull" : gate === "bear" ? "text-status-bear" : "text-status-watch";
  const confirmed = isConfirmed(axis.verdict);
  return (
    <>
      {/* A BUTTON, NOT A `title` SPAN (#388). `title` renders as a hover tooltip on a desktop and does
          NOTHING on a touch device, so on the surface the operator actually uses a dot could not be identified
          at all. A button is tappable, focusable and reachable by keyboard for free — the design-system
          note requires the last of those, and the old span satisfied none of them.

          The 2px dot keeps its size; the HIT AREA is a 24px transparent box around it, because a 8px
          target is below every touch guideline and this is the interaction the issue is about. */}
      <button
        type="button"
        onClick={onSelect}
        aria-label={`${axis.pair} — ${axis.label} · ${point.quadrant}`}
        aria-pressed={selected}
        className="absolute grid h-6 w-6 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full focus:outline-none focus-visible:ring-1 focus-visible:ring-t2"
        style={{ left: px(point.x), top: py(point.y) }}
      >
        <span
          className={`h-2 w-2 rounded-full ring-[1.5px] ring-ds-bg ${tone} ${
            confirmed ? "bg-current" : "border-2 border-current bg-ds-bg"
          } ${selected ? "scale-150" : ""}`}
        />
      </button>
      {side && (
        <span
          className={`pointer-events-none absolute whitespace-nowrap font-mono text-[8px] ${tone}`}
          style={{
            left: px(point.x),
            top: py(point.y),
            // FOUR PLACEMENTS. The geometry that chose the side lives in `placeLabels`; this only has
            // to render the same offsets it measured, so the two cannot disagree about where a label
            // actually sits — which is how a label ends up drawn somewhere the collision test never
            // considered.
            transform:
              side === "left"
                ? "translate(calc(-100% - 6px), -50%)"
                : side === "right"
                  ? "translate(6px, -50%)"
                  : side === "above"
                    ? "translate(-50%, calc(-100% - 6px))"
                    : "translate(-50%, 6px)",
          }}
        >
          {axis.pair}
        </span>
      )}
    </>
  );
}

export function MomentumMap({
  axes,
  windowKey,
  height = 224,
}: {
  axes: RotationAxis[];
  windowKey: string;
  /** Plot height in px — a prop with a default, the shape `EquityCurve` already uses. */
  height?: number;
}) {
  const wins: Array<RotationWindow | null | undefined> = axes.map((a) => a.win?.[windowKey]);
  const scale = mapScale(wins);
  const points = axes
    .map((axis) => ({ axis, point: mapPoint(axis.win?.[windowKey], scale) }))
    .filter((p): p is { axis: RotationAxis; point: MapPoint } => p.point !== null);
  const { separable, plotted } = separableCount(wins);

  // MEASURED, because collision is a question about pixels. One ResizeObserver, one number.
  const plotRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = plotRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    ro.observe(el);
    setWidth(el.getBoundingClientRect().width);
    return () => ro.disconnect();
  }, []);

  const [selected, setSelected] = useState<string | null>(null);

  // NAMED BY DISTANCE FROM THE ORIGIN, which is what the reference does — the axes that have actually
  // gone somewhere. Naming only the statistically separable ones (the first cut) meant that on every
  // window except 1D the map carried no labels at all.
  //
  // The cap is now a CEILING, not a quota: `placeLabels` walks this list in priority order and drops
  // anyone who cannot be drawn legibly, so a crowded window shows fewer than the cap rather than
  // stacking them. That is #388's requirement — the dot's position is still information without a name.
  const ranked = [...points]
    .sort(
      (a, b) =>
        Math.abs(b.point.x) + Math.abs(b.point.y) - (Math.abs(a.point.x) + Math.abs(a.point.y)),
    )
    .slice(0, height < 260 ? NAMED_NARROW : NAMED);
  const placed = new Map(
    placeLabels(
      ranked.map((p) => ({ pair: p.axis.pair, x: p.point.x, y: p.point.y })),
      { width, height },
      // EVERY plotted point, not just the ranked ones — an unlabelled marker is still something a
      // label must not be written through. This is what the operator was looking at: `GLD/SPY` printed
      // straight through a dot.
      points.map((p) => ({ x: p.point.x, y: p.point.y })),
    ).map((pl) => [pl.pair, pl.side]),
  );
  const chosen = points.find((p) => p.axis.pair === selected) ?? null;

  if (plotted === 0) {
    // Nothing plottable is not an empty market — same rule the tile's own absence branch follows.
    return (
      <div className="rounded-md border border-dashed border-ds-line px-3 py-6 text-center font-mono text-[11px] text-t3">
        No drift and travel for {windowKey} — nothing to place on the map
      </div>
    );
  }

  return (
    <div>
      {/* THE CAVEAT IS STATED ONCE, IN WORDS, rather than drawn twenty-five times. On most windows the
          honest answer is "none of it", and an operator deciding whether to act needs that before they
          read a single dot — but they need it as a sentence, not as a thicket. */}
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-x-2 px-1">
        <span className="font-mono text-[10px] text-t3">drift × travel · {windowKey}</span>
        <span className={`font-mono text-[10px] ${separable === 0 ? "text-t3" : "text-t2"}`}>
          {separable === 0
            ? `none of ${plotted} separable from zero — positions, not verdicts`
            : `${separable} of ${plotted} separable from zero`}
        </span>
      </div>

      <div
        ref={plotRef}
        className="relative w-full overflow-hidden rounded-md border border-ds-line"
        style={{ height }}
      >
        <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-ds-line" aria-hidden />
        <div className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-ds-line" aria-hidden />
        <Corner label="Improving" at="left-1.5 top-1" />
        <Corner label="Leading" at="right-1.5 top-1" />
        <Corner label="Lagging" at="left-1.5 bottom-1" />
        <Corner label="Weakening" at="right-1.5 bottom-1" />
        {points.map(({ axis, point }) => (
          <Dot
            key={axis.pair}
            axis={axis}
            point={point}
            side={placed.get(axis.pair) ?? null}
            selected={selected === axis.pair}
            onSelect={() => setSelected((cur) => (cur === axis.pair ? null : axis.pair))}
          />
        ))}
      </div>

      {/* WHERE A TAP GETS ITS ANSWER. `title` could not do this on a phone, and an unlabelled dot had no
          way to identify itself at all. A fixed-height line so selecting does not reflow the tile —
          the map jumping under the finger that just tapped it is its own bug. */}
      <div className="mt-1 min-h-[1.1rem] px-1 font-mono text-[10px] leading-tight">
        {chosen ? (
          <span className="text-t2">
            <span className="font-semibold text-t1">{chosen.axis.pair}</span> · {chosen.axis.label} ·{" "}
            {chosen.point.quadrant}
            {chosen.point.separable ? "" : " · interval crosses zero"}
          </span>
        ) : (
          <span className="text-t3">tap a dot to name it</span>
        )}
      </div>
    </div>
  );
}
