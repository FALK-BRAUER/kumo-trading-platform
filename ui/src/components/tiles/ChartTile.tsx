"use client";

/**
 * ChartTile — full Ichimoku candlestick chart, ported from kumo-trader/ui's IchimokuChart and driven
 * by the cockpit WS snapshot (one instrument). Renders candles + Tenkan/Kijun + Senkou Span A/B
 * (the kumo cloud) + Chikou + MA200 + the entry price line, with a timeframe bar and fullscreen.
 *
 * The conversion/cloud lines are computed client-side from the bar history (the snapshot carries
 * ~260 bars/ticker, enough for Span B 52 and MA200 200 to form). Timeframe buttons are scaffolding:
 * all map to the daily snapshot until the #11 market-data provider supplies real multi-TF series.
 */
import { useEffect, useRef, useState } from "react";
import { Maximize2, Minimize2 } from "lucide-react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { BarDTO, FillDTO } from "@/lib/api/types";
import { buildCloudData, cloudSeriesView } from "@/lib/chart/cloudSeries";
import { exitReason, markerLabel, trailLines } from "@/lib/chart/trail";
import type { SessionFrame } from "@/lib/framework/datasource/protocol";
import { useSource } from "@/lib/framework/datasource/useSource";
import { useInstrument } from "@/lib/framework/instrument";

// Multi-timeframe chart (#58). Two independent axes: RANGE (how far back you look) and CANDLE (bar
// granularity). Picking a range snaps to a sensible DEFAULT candle, but EVERY candle stays selectable —
// you can always drop to 1m or up to 1w. Candle drives the actual per-granularity data fetch
// (useInstrument); range drives the visible time window (span ÷ candle → bar count). Extreme combos
// (1m-on-5Y) are bounded by the backend's per-granularity history lookback, not the UI.
type Range = "1D" | "1W" | "1M" | "3M" | "1Y" | "5Y";
type Candle = "1m" | "5m" | "15m" | "30m" | "1h" | "1d" | "1w";
const RANGES: Range[] = ["1D", "1W", "1M", "3M", "1Y", "5Y"];
const CANDLES: Candle[] = ["1m", "5m", "15m", "30m", "1h", "1d", "1w"];

// Range span in trading days, and each range's landing candle.
const RANGE_DAYS: Record<Range, number> = { "1D": 1, "1W": 5, "1M": 21, "3M": 63, "1Y": 252, "5Y": 1260 };
const RANGE_DEFAULT_CANDLE: Record<Range, Candle> = {
  "1D": "5m", "1W": "30m", "1M": "1h", "3M": "1d", "1Y": "1w", "5Y": "1w",
};
// Approx bars per trading day per candle (RTH ≈ 6.5h) — turns a range span into a visible bar count so the
// window shows the same wall-clock span at any candle (3M×1d → 63 bars; 3M×1h → ~440; 1D×1m → 390).
const BARS_PER_DAY: Record<Candle, number> = { "1m": 390, "5m": 78, "15m": 26, "30m": 13, "1h": 7, "1d": 1, "1w": 0.2 };

// Intraday candles need the time-of-day on the axis; daily/weekly don't.
const isIntraday = (c: Candle): boolean => c.endsWith("m") || c === "1h";

// US-equity charts read in MARKET time (ET), not the browser/UTC clock. Without this the regular session
// (09:30–16:00 ET) shows as 13:30–20:00 UTC, which is baffling. lightweight-charts has no native timezone,
// so we format the UTC timestamp into America/New_York for both the axis ticks and the crosshair label.
const _ET_TIME = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false });
const _ET_DATE = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric" });
const _ET_FULL = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
// tickMarkType: 0=Year 1=Month 2=DayOfMonth 3=Time 4=TimeWithSeconds — ≥3 is a time-of-day tick.
const etTickFormatter = (time: Time, tickMarkType: number): string => {
  const d = new Date((time as number) * 1000);
  return tickMarkType >= 3 ? _ET_TIME.format(d) : _ET_DATE.format(d);
};
const etCrosshairFormatter = (time: Time): string => `${_ET_FULL.format(new Date((time as number) * 1000))} ET`;
const visibleBarsFor = (range: Range, candle: Candle): number =>
  Math.max(10, Math.round(RANGE_DAYS[range] * BARS_PER_DAY[candle]));

const nsToSec = (ns: number): UTCTimestamp => Math.floor(ns / 1e9) as UTCTimestamp;

type Line = { time: Time; value: number }[];

/** Midpoint of highest-high / lowest-low over `period` bars ending at each index. */
function conversion(bars: BarDTO[], period: number): Line {
  const out: Line = [];
  for (let i = period - 1; i < bars.length; i += 1) {
    const w = bars.slice(i - period + 1, i + 1);
    out.push({
      time: nsToSec(bars[i].ts_event),
      value: (Math.max(...w.map((b) => b.high)) + Math.min(...w.map((b) => b.low))) / 2,
    });
  }
  return out;
}

/** (Tenkan + Kijun) / 2 per bar — Senkou Span A (cloud upper/lower vs Span B). */
function spanA(bars: BarDTO[]): Line {
  const out: Line = [];
  for (let i = 25; i < bars.length; i += 1) {
    const tw = bars.slice(i - 8, i + 1);
    const kw = bars.slice(i - 25, i + 1);
    const t = (Math.max(...tw.map((b) => b.high)) + Math.min(...tw.map((b) => b.low))) / 2;
    const k = (Math.max(...kw.map((b) => b.high)) + Math.min(...kw.map((b) => b.low))) / 2;
    out.push({ time: nsToSec(bars[i].ts_event), value: (t + k) / 2 });
  }
  return out;
}

/** Simple moving average of close over `period`. */
function sma(bars: BarDTO[], period: number): Line {
  const out: Line = [];
  let sum = 0;
  for (let i = 0; i < bars.length; i += 1) {
    sum += bars[i].close;
    if (i >= period) sum -= bars[i - period].close;
    if (i >= period - 1) out.push({ time: nsToSec(bars[i].ts_event), value: sum / period });
  }
  return out;
}

/** Project a span forward by `displacement` bars — standard Ichimoku plots Senkou A/B AHEAD of price,
 *  which is what makes the cloud lead. Without it the cloud sits on the current bar, so it reads as
 *  today's cloud while actually being the FUTURE one, and price-vs-cloud is 26 bars out.
 *
 *  The last `displacement` points have no bar to hang on yet, so their timestamps are extrapolated
 *  from the median bar interval. Median, not last-minus-previous: a session gap or a half-day would
 *  otherwise set the spacing for the entire projection.
 *
 *  Note this deliberately differs from `lastLevels()` in lib/ichimoku.ts, which is UNSHIFTED by
 *  design for the #179/#180 cloud chips. The chart matches `shiftedCloud()` — the scanner-exact
 *  reading #181's Blue Flag uses — so what is drawn agrees with what the checklist evaluates.
 */
function displace(line: Line, bars: BarDTO[], displacement = 26): Line {
  if (line.length === 0 || bars.length < 2) return line;
  const gaps: number[] = [];
  for (let i = 1; i < bars.length; i += 1) gaps.push(nsToSec(bars[i].ts_event) - nsToSec(bars[i - 1].ts_event));
  gaps.sort((a, b) => a - b);
  const step = gaps[Math.floor(gaps.length / 2)] || 1;
  const lastTs = nsToSec(bars[bars.length - 1].ts_event);
  return line.map((pt, i) => {
    const target = i + displacement;
    const time = target < bars.length ? nsToSec(bars[target].ts_event) : lastTs + (target - bars.length + 1) * step;
    return { time, value: pt.value } as Line[number];
  });
}

/** Chikou span: close plotted 26 bars back. */
function chikou(bars: BarDTO[]): Line {
  const out: Line = [];
  for (let i = 26; i < bars.length; i += 1) {
    out.push({ time: nsToSec(bars[i - 26].ts_event), value: bars[i].close });
  }
  return out;
}

function fillMarkers(fills: FillDTO[], session: SessionFrame | null,
                     instrumentId: string): SeriesMarker<Time>[] {
  return fills.map((f) => ({
    time: nsToSec(f.ts_event),
    position: f.side === "BUY" ? "belowBar" : "aboveBar",
    color: f.side === "BUY" ? "#10b981" : "#ef4444",
    shape: f.side === "BUY" ? "arrowUp" : "arrowDown",
    // Resolved PER FILL, passing that fill's timestamp: the frame carries the latest decision, which
    // is not necessarily today's, and these fills are filtered by instrument alone. One reason
    // applied to every sell would label a three-week-old exit with this morning's rationale.
    text: markerLabel(f.side, f.quantity, f.price,
                      f.side === "SELL" ? exitReason(session, instrumentId, f.ts_event) : ""),
  }));
}

/**
 * Chart chrome (background / grid / axis text / borders) reads the resolved design tokens so it follows the
 * app theme. lightweight-charts needs concrete color strings, not CSS vars — so we resolve them here and
 * re-apply on theme change (see the MutationObserver effect). Candle/line colors are semantic (green/red/
 * blue …) and stay fixed across themes. Fallbacks are the original dark values (SSR / var not yet applied).
 */
function chartChrome() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return {
    bg: v("--ds-surf", "#09090b"),
    line: v("--ds-line", "#27272a"),
    text: v("--ds-t2", "#a1a1aa"),
  };
}

interface Props {
  instrumentId: string;
  entryPrice?: number;
  /** Fill the parent's height instead of the fixed 320px (for the full-region detail surface #27).
   *  The parent must give this a bounded height (e.g. a flex column). */
  fill?: boolean;
}

export function ChartTile({ instrumentId, entryPrice, fill = false }: Props) {
  const [range, setRange] = useState<Range>("3M");
  const [candle, setCandle] = useState<Candle>(RANGE_DEFAULT_CANDLE["3M"]);
  const [fullscreen, setFullscreen] = useState(false);

  // The strategy's own state (#212) — the exit trail and the journal's per-symbol reasons. Read
  // directly rather than declared as a tile dataSource because it annotates an existing chart rather
  // than driving it: with no frame the chart renders exactly as before.
  const { data: sessionData } = useSource("session", undefined);
  const session = (sessionData ?? null) as SessionFrame | null;

  // Self-fetch bars + fills at the SELECTED candle (#58) — the chart owns its granularity, so the
  // per-tile candle drives a real per-granularity data fetch instead of a client-side zoom over daily.
  const { bars, fills, status } = useInstrument(instrumentId, candle);

  // Pick a range → snap to its default candle (every candle is still hand-selectable afterwards).
  const selectRange = (r: Range) => {
    setRange(r);
    setCandle(RANGE_DEFAULT_CANDLE[r]);
  };
  const visibleBars = visibleBarsFor(range, candle);

  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const cloudRef = useRef<ISeriesApi<"Custom"> | null>(null);
  const lineRefs = useRef<Record<string, ISeriesApi<"Line">>>({});
  // True → the NEXT paint should snap the visible range to the window (initial load, or after the user
  // changes range/candle/fullscreen). False during live-tick repaints so a manual scroll/pan is never reset;
  // lightweight-charts auto-scrolls new bars on its own.
  const fitPendingRef = useRef(true);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  // Latest data kept in a ref so paint() can run from the build effect — this survives StrictMode's
  // dev mount→unmount→remount, which otherwise lands the WS data on the first (discarded) chart and
  // leaves the second chart empty/blank because the data effect's deps haven't changed.
  const dataRef = useRef({ bars, fills, visibleBars, session });
  dataRef.current = { bars, fills, visibleBars, session };

  const paint = () => {
    const series = candleRef.current;
    if (!series) return;
    const { bars: bs, fills: fs, visibleBars } = dataRef.current;
    if (bs.length === 0) {
      // Candle switched → the new granularity's series is momentarily empty (or the symbol streams none).
      // CLEAR every series instead of returning, so we never leave the PREVIOUS candle's candlesticks on
      // screen under the new candle's axis + footer (stale-chart bug). Repaints when history/live arrives.
      series.setData([]);
      cloudRef.current?.setData([]);
      Object.values(lineRefs.current).forEach((l) => l?.setData([]));
      markersRef.current?.setMarkers([]);
      return;
    }
    series.setData(
      bs.map((b) => ({ time: nsToSec(b.ts_event), open: b.open, high: b.high, low: b.low, close: b.close })),
    );
    // Displaced forward 26, per standard Ichimoku. Both spans must use the SAME displacement or the
    // cloud fill between them is built from two different points in time.
    const spanAData = displace(spanA(bs), bs);
    const spanBData = displace(conversion(bs, 52), bs);
    lineRefs.current.tenkan?.setData(conversion(bs, 9));
    lineRefs.current.kijun?.setData(conversion(bs, 26));
    lineRefs.current.spanA?.setData(spanAData);
    lineRefs.current.spanB?.setData(spanBData);
    // The cloud fill sits between the two spans (aligned by time) — reuse the exact line data.
    cloudRef.current?.setData(buildCloudData(spanAData, spanBData));
    lineRefs.current.chikou?.setData(chikou(bs));
    lineRefs.current.ma200?.setData(sma(bs, 200));
    markersRef.current?.setMarkers(fillMarkers(fs, dataRef.current.session, instrumentId));
    // Snap the window ONLY when a fit is pending (initial load / intentional range change) — never on a
    // live-tick repaint, or the user's scroll would reset every tick.
    const n = bs.length;
    if (fitPendingRef.current && n > 0) {
      const visible = Math.min(n, visibleBars);
      chartRef.current?.timeScale().setVisibleLogicalRange({ from: n - visible, to: n - 1 });
      fitPendingRef.current = false;
    }
  };

  // Build chart once.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const c = chartChrome();
    const chart = createChart(el, {
      layout: { background: { type: ColorType.Solid, color: c.bg }, textColor: c.text },
      grid: { vertLines: { color: c.line }, horzLines: { color: c.line } },
      crosshair: { mode: 1 },
      // Price scale on the LEFT (the operator's steer) — the last-value tags (Tenkan/Kijun/Span A/B/Chikou) were
      // crowding the right edge over the most recent candles.
      leftPriceScale: { visible: true, borderColor: c.line },
      rightPriceScale: { visible: false },
      // Axis + crosshair times in market time (ET), not UTC. See etTickFormatter.
      localization: { timeFormatter: etCrosshairFormatter },
      timeScale: { borderColor: c.line, timeVisible: false, tickMarkFormatter: etTickFormatter },
      width: el.clientWidth || 600,
      height: 320,
    });
    chartRef.current = chart;

    // Explicitly size to the container (autoSize didn't fire reliably inside the expanding row).
    const fit = () => {
      const w = el.clientWidth;
      if (w > 0) chart.resize(w, el.clientHeight || 320);
    };
    requestAnimationFrame(fit);
    const ro = new ResizeObserver(fit);
    ro.observe(el);

    // The Ichimoku cloud (kumo) — added FIRST so its fill draws BEHIND the candles + lines. Custom series
    // (lightweight-charts has no native fill-between-lines). No last-value tag (it's a band, not a level).
    cloudRef.current = chart.addCustomSeries(cloudSeriesView(), { priceScaleId: "left", lastValueVisible: false, priceLineVisible: false });

    // All series on the "left" scale so their price + last-value tags render on the left axis.
    candleRef.current = chart.addSeries(CandlestickSeries, {
      priceScaleId: "left",
      upColor: "#10b981",
      downColor: "#ef4444",
      borderUpColor: "#10b981",
      borderDownColor: "#ef4444",
      wickUpColor: "#10b981",
      wickDownColor: "#ef4444",
    });
    lineRefs.current = {
      tenkan: chart.addSeries(LineSeries, { priceScaleId: "left", color: "#3b82f6", lineWidth: 1, title: "Tenkan" }),
      kijun: chart.addSeries(LineSeries, { priceScaleId: "left", color: "#f59e0b", lineWidth: 1, title: "Kijun" }),
      spanA: chart.addSeries(LineSeries, {
        priceScaleId: "left",
        color: "rgba(16,185,129,0.6)",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        title: "Span A",
      }),
      spanB: chart.addSeries(LineSeries, {
        priceScaleId: "left",
        color: "rgba(239,68,68,0.6)",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        title: "Span B",
      }),
      chikou: chart.addSeries(LineSeries, {
        priceScaleId: "left",
        color: "rgba(156,163,175,0.6)",
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        title: "Chikou",
      }),
      // 200MA on intraday sits ~100h behind price (far below), which would stretch the price axis and crush
      // the candles. autoscaleInfoProvider: () => null → the line still draws, but no longer contributes to
      // the vertical autoscale, so the axis fits the candles + cloud and bars stay readable (#chart readability).
      ma200: chart.addSeries(LineSeries, {
        priceScaleId: "left",
        color: "rgba(168,85,247,0.8)",
        lineWidth: 1,
        title: "200MA",
        autoscaleInfoProvider: () => null,
      }),
    };
    markersRef.current = createSeriesMarkers(candleRef.current, []);
    paint(); // apply any data that already arrived (covers the StrictMode remount)

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  // Chart chrome follows the app theme (#49 Light/Dark/System). lightweight-charts holds concrete colors,
  // so re-resolve the tokens and re-apply whenever the theme class on <html> flips (manual toggle OR an OS
  // change while in "system"). Runs once on mount too, so a chart built before the theme class settled still
  // corrects itself.
  useEffect(() => {
    const apply = () => {
      const c = chartChrome();
      chartRef.current?.applyOptions({
        layout: { background: { type: ColorType.Solid, color: c.bg }, textColor: c.text },
        grid: { vertLines: { color: c.line }, horzLines: { color: c.line } },
        leftPriceScale: { borderColor: c.line },
        timeScale: { borderColor: c.line },
      });
    };
    apply();
    const obs = new MutationObserver(apply);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  // Entry price line — recreated when entryPrice changes.
  useEffect(() => {
    const series = candleRef.current;
    if (!series || entryPrice == null) return;
    const line = series.createPriceLine({
      price: entryPrice,
      color: "#f59e0b",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: "entry",
    });
    return () => series.removePriceLine(line);
  }, [entryPrice]);

  // Exit-trail levels from the session frame (#212). The entry line above comes from the tile's own
  // prop, so only levels it does not already draw are added here — chiefly the PEAK, which is what
  // the give-back rule actually measures against and which nothing on this chart has ever shown.
  //
  // An ADOPTED trail contributes no peak line at all: that peak was never observed, and drawing it
  // would put a fabricated level on the chart in the same visual language as a measured one.
  useEffect(() => {
    const series = candleRef.current;
    if (!series) return;
    const lines = trailLines(session, instrumentId)
      .filter((l) => !(l.title === "entry" && entryPrice != null));
    const handles = lines.map((l) => series.createPriceLine({
      price: l.price,
      color: l.color,
      lineWidth: 1,
      lineStyle: l.style === "dashed" ? LineStyle.Dashed : LineStyle.Solid,
      axisLabelVisible: true,
      title: l.title,
    }));
    return () => handles.forEach((h) => series.removePriceLine(h));
  }, [session, instrumentId, entryPrice]);

  // Time axis depends only on the candle (intraday → show time-of-day) — apply on candle change, NOT on
  // every live-bar increment.
  useEffect(() => {
    chartRef.current?.applyOptions({ timeScale: { timeVisible: isIntraday(candle), secondsVisible: false } });
  }, [candle]);

  // Repaint on data / range / candle change.
  useEffect(() => {
    paint();
  }, [bars, fills, range, candle, session]);

  // Request a one-time re-fit when the user changes range/candle/fullscreen (NOT on every new bar — that was
  // resetting a manual scroll each live tick). Fit now if data is present, else leave it pending for the next
  // paint once the new candle's data lands.
  useEffect(() => {
    fitPendingRef.current = true;
    const raf = requestAnimationFrame(() => {
      if (!fitPendingRef.current) return;
      const { bars: bs, visibleBars } = dataRef.current;
      const n = bs.length;
      if (n === 0) return;
      const visible = Math.min(n, visibleBars);
      chartRef.current?.timeScale().setVisibleLogicalRange({ from: n - visible, to: n - 1 });
      fitPendingRef.current = false;
    });
    return () => cancelAnimationFrame(raf);
  }, [fullscreen, range, candle]);

  const last = bars.length ? bars[bars.length - 1] : null;

  const rootClass = fullscreen
    ? "fixed inset-0 z-50 bg-ds-bg flex flex-col"
    : fill
      ? "flex h-full flex-col"
      : "";

  return (
    <div className={rootClass}>
      {/* Header: ticker + timeframe bar + fullscreen */}
      <div className="flex items-center justify-between gap-2 px-3 py-1.5 border-b border-ds-line/50">
        {/* Range + candle share one line when it fits; on a narrow screen the candle bar WRAPS to the next
            line rather than scrolling options off-screen (so the active candle is never hidden). Both bars
            share one color scheme — active = filled zinc, inactive = muted — so they read as siblings. */}
        <div className="flex flex-wrap items-center gap-1.5 min-w-0">
          {/* Range bar — picking a range snaps to its default candle. */}
          <div className="flex shrink-0 overflow-hidden rounded border border-ds-line2">
            {RANGES.map((r) => (
              <button
                key={r}
                onClick={() => selectRange(r)}
                className={`px-1.5 py-0.5 font-mono text-[11px] transition-colors ${
                  range === r ? "bg-ds-line2 font-semibold text-t1" : "text-t2 hover:bg-ds-surf2 hover:text-t1"
                }`}
              >
                {r}
              </button>
            ))}
          </div>
          {/* Candle bar — all granularities always selectable; range sets the default. */}
          <div className="flex shrink-0 overflow-hidden rounded border border-ds-line2">
            {CANDLES.map((c) => (
              <button
                key={c}
                onClick={() => setCandle(c)}
                className={`px-1.5 py-0.5 font-mono text-[11px] transition-colors ${
                  candle === c ? "bg-ds-line2 font-semibold text-t1" : "text-t2 hover:bg-ds-surf2 hover:text-t1"
                }`}
              >
                {c}
              </button>
            ))}
          </div>
        </div>
        <button
          onClick={() => setFullscreen((v) => !v)}
          className="text-t3 hover:text-t1 p-1 rounded hover:bg-ds-surf2 transition-colors"
          aria-label={fullscreen ? "Exit fullscreen" : "Fullscreen"}
        >
          {fullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
        </button>
      </div>

      {/* Legend */}
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 px-3 py-1 text-[11px] font-mono border-b border-ds-line/50">
        <span className="text-status-info">— Tenkan</span>
        <span className="text-status-watch">— Kijun</span>
        <span className="text-status-bull">-- Span A</span>
        <span className="text-status-bear">-- Span B</span>
        <span className="text-t2">·· Chikou</span>
        <span style={{ color: "rgb(168,85,247)" }}>— 200MA</span>
        <span className="ml-auto text-t3">
          {bars.length} × {candle}{last ? ` · ${last.close.toFixed(2)}` : ""}
        </span>
      </div>

      <div className={`relative ${fullscreen || fill ? "w-full flex-1 min-h-0" : "w-full h-[320px]"}`}>
        <div ref={containerRef} className="h-full w-full" />
        {/* Per-candle loading state (#58): the chart owns its own granularity fetch, so show loading here
            when the selected candle has no bars yet — never a bare blank or the previous candle's data. */}
        {bars.length === 0 && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center font-mono text-[11px] text-t3">
            {status === "error" ? `no ${candle} feed` : `loading ${candle}…`}
          </div>
        )}
      </div>
    </div>
  );
}
