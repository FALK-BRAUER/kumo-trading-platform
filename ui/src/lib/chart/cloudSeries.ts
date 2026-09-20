/**
 * Ichimoku kumo (cloud) as a lightweight-charts v5 CUSTOM SERIES (#chart). lightweight-charts has no native
 * "fill between two lines", so the cloud — the filled region between Senkou Span A and Span B — is drawn by a
 * custom pane renderer: for each bar-to-bar segment it fills the quad between the two spans, coloured bull
 * (green) where A ≥ B and bear (red) where B > A, splitting the fill at the exact crossover so the colour
 * flips on the right pixel. Pure geometry + canvas; the Span A/B lines stay as the cloud edges.
 */
import {
  customSeriesDefaultOptions,
  type CustomData,
  type CustomSeriesOptions,
  type CustomSeriesPricePlotValues,
  type ICustomSeriesPaneRenderer,
  type ICustomSeriesPaneView,
  type PaneRendererCustomData,
  type PriceToCoordinateConverter,
  type Time,
  type WhitespaceData,
} from "lightweight-charts";
// fancy-canvas is lightweight-charts' own rendering-target dep; CanvasRenderingTarget2D is declared there and
// not re-exported by the main entry. Type-only import (erased at build).
import type { CanvasRenderingTarget2D } from "fancy-canvas";

/** One cloud point: the two Senkou spans at a time. Whitespace (no fill) when either is missing. */
export interface CloudData extends CustomData<Time> {
  a: number; // Senkou Span A
  b: number; // Senkou Span B
}

export interface CloudSeriesOptions extends CustomSeriesOptions {
  bullColor: string; // A >= B
  bearColor: string; // B > A
}

const DEFAULTS: CloudSeriesOptions = {
  ...customSeriesDefaultOptions,
  bullColor: "rgba(16,185,129,0.15)",
  bearColor: "rgba(239,68,68,0.15)",
  lastValueVisible: false,
  priceLineVisible: false,
};

const isCloud = (d: CloudData | WhitespaceData<Time>): d is CloudData =>
  (d as CloudData).a != null && (d as CloudData).b != null;

class CloudRenderer implements ICustomSeriesPaneRenderer {
  _data: PaneRendererCustomData<Time, CloudData> | null = null;
  _options: CloudSeriesOptions | null = null;

  update(data: PaneRendererCustomData<Time, CloudData>, options: CloudSeriesOptions): void {
    this._data = data;
    this._options = options;
  }

  draw(target: CanvasRenderingTarget2D, priceConverter: PriceToCoordinateConverter): void {
    const data = this._data;
    const options = this._options;
    if (!data || !options || data.bars.length < 2 || data.visibleRange === null) return;

    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context;
      const hr = scope.horizontalPixelRatio;
      const vr = scope.verticalPixelRatio;
      // Extend one segment beyond the visible range on each side so the fill doesn't clip at the edges.
      const from = Math.max(0, data.visibleRange!.from - 1);
      const to = Math.min(data.bars.length - 1, data.visibleRange!.to + 1);

      for (let i = from; i < to; i++) {
        const l = data.bars[i];
        const r = data.bars[i + 1];
        const ld = l.originalData;
        const rd = r.originalData;
        if (ld == null || rd == null || !isCloud(ld) || !isCloud(rd)) continue;

        const x1 = l.x * hr;
        const x2 = r.x * hr;
        const ya1 = priceConverter(ld.a);
        const yb1 = priceConverter(ld.b);
        const ya2 = priceConverter(rd.a);
        const yb2 = priceConverter(rd.b);
        if (ya1 === null || yb1 === null || ya2 === null || yb2 === null) continue;

        const d1 = ld.a - ld.b; // sign in PRICE space (unambiguous vs inverted y)
        const d2 = rd.a - rd.b;

        const fillQuad = (
          xa: number,
          yaL: number,
          xb: number,
          yaR: number,
          ybR: number,
          ybL: number,
          bull: boolean,
        ) => {
          ctx.beginPath();
          ctx.moveTo(xa, yaL * vr);
          ctx.lineTo(xb, yaR * vr);
          ctx.lineTo(xb, ybR * vr);
          ctx.lineTo(xa, ybL * vr);
          ctx.closePath();
          ctx.fillStyle = bull ? options.bullColor : options.bearColor;
          ctx.fill();
        };

        // No crossover in this segment → one quad, coloured by the segment's sign.
        if (d1 === 0 || d2 === 0 || d1 * d2 > 0) {
          fillQuad(x1, ya1, x2, ya2, yb2, yb1, d1 + d2 >= 0);
          continue;
        }

        // Crossover: split at the intersection. t in [0,1] where (a−b) hits zero. Cross price = A at t.
        const t = d1 / (d1 - d2);
        const xc = x1 + t * (x2 - x1);
        const crossPrice = ld.a + t * (rd.a - ld.a);
        const yc = priceConverter(crossPrice);
        if (yc === null) {
          fillQuad(x1, ya1, x2, ya2, yb2, yb1, d1 + d2 >= 0);
          continue;
        }
        // Left triangle (x1 side) coloured by d1; right triangle (x2 side) by d2. Both collapse to yc at xc.
        fillQuad(x1, ya1, xc, yc, yc, yb1, d1 > 0);
        fillQuad(xc, yc, x2, ya2, yb2, yc, d2 > 0);
      }
    });
  }
}

/** ICustomSeriesPaneView for the cloud. Register with `chart.addCustomSeries(cloudSeriesView(), { … })`. */
export function cloudSeriesView(): ICustomSeriesPaneView<Time, CloudData, CloudSeriesOptions> {
  const renderer = new CloudRenderer();
  return {
    priceValueBuilder: (row: CloudData): CustomSeriesPricePlotValues => [row.b, row.a], // autoscale to both spans
    isWhitespace: (d): d is WhitespaceData<Time> => !isCloud(d as CloudData),
    renderer: () => renderer,
    update: (data, options) => renderer.update(data, options),
    defaultOptions: () => DEFAULTS,
  };
}

/** Zip the Span A + Span B line series (each `{time, value}`) into cloud points aligned by time. A time present
 *  in only one span becomes whitespace (no fill there). Pure — unit-tested. */
export function buildCloudData(
  spanA: { time: Time; value: number }[],
  spanB: { time: Time; value: number }[],
): (CloudData | WhitespaceData<Time>)[] {
  const bByTime = new Map<string, number>();
  for (const p of spanB) bByTime.set(String(p.time), p.value);
  const out: (CloudData | WhitespaceData<Time>)[] = [];
  for (const p of spanA) {
    const b = bByTime.get(String(p.time));
    out.push(b != null ? { time: p.time, a: p.value, b } : { time: p.time });
  }
  return out;
}
