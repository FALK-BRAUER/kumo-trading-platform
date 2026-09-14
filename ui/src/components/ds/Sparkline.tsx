/**
 * Sparkline (#104 / #114) — a tiny trend polyline for a list row's "Trend" cell. Watch-only per the
 * row/format contract. Colour = direction (last vs first) on the semantic tokens (day/night aware).
 *
 * `fluid` (#294) makes the chart take the width its container gives it instead of a fixed pixel count.
 * The trend strip has six of these side by side and the row it sits in is 390px on a phone and much
 * wider on a desktop; picking any one number is wrong on one of them. Operator, after the fixed-24px
 * version: "can't make the trendlines use the available width and have a max height for desktop".
 *
 * Fluid mode keeps the same geometry maths and hands scaling to the viewBox. `preserveAspectRatio`
 * is "none" ON PURPOSE — a sparkline is not a shape, it is two independent axes, and letting the
 * browser letterbox it would leave dead margin in a cell whose whole job is to be narrow. The stroke
 * is pinned with `vector-effect="non-scaling-stroke"` so the line does not thicken or thin as the
 * column resizes, which is what a naive viewBox scale does to it.
 */
export function Sparkline({
  values,
  width = 60,
  height = 16,
  className = "",
  fluid = false,
}: {
  values: number[];
  width?: number;
  height?: number;
  className?: string;
  /** Fill the container's width; `width` becomes the viewBox's coordinate space, not a pixel size. */
  fluid?: boolean;
}) {
  // Sized by CSS in fluid mode, by attributes otherwise. An empty series must still occupy the cell —
  // a zero-width placeholder would let the five covered windows redistribute and the strip would
  // reflow as history streamed in.
  const box = fluid
    ? ({ viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none" as const, className: `w-full ${className}` })
    : ({ width, height, className });
  if (values.length < 2) return <svg {...box} style={fluid ? { height } : undefined} aria-hidden />;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pts = values
    .map((v, i) => `${(i / (values.length - 1)) * width},${height - ((v - min) / span) * height}`)
    .join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg {...box} style={fluid ? { height } : undefined} aria-hidden>
      <polyline
        points={pts}
        fill="none"
        strokeWidth={1.2}
        vectorEffect={fluid ? "non-scaling-stroke" : undefined}
        className={up ? "stroke-status-bull" : "stroke-status-bear"}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
