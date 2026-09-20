"use client";

/**
 * useInstrument (#29) — the ONE blessed way a tile touches per-symbol market data. Every symbol-bearing
 * tile (watchlist row, position row, detail surface) calls this and nothing else; they never reach the raw
 * transport hooks (`useSource`/live-price), so two tiles showing the same symbol behave identically by
 * construction. The mark-price / status / fills semantics live HERE, once — not copied into each tile.
 *
 * P&L is deliberately NOT here: it's position-scoped (one symbol can have N positions). Use `computePnl`
 * with a position + this hook's `price`.
 */
import { useCallback, useMemo, useSyncExternalStore } from "react";
import { useSource } from "./datasource/useSource";
import { wsManager } from "./datasource/ws-manager";
import { canonicalTopicKey, type Topic } from "./datasource/protocol";
import type { SourceStatus } from "./types";
import type { BarDTO, FillDTO, PositionDTO } from "@/lib/api/types";
import { sideSign, signedQty } from "./signedQty";

interface LivePriceRow {
  instrument_id: string;
  price: number;
  size: number;
  ts_event: number;
}
interface QuoteRow {
  instrument_id: string;
  bid: number;
  ask: number;
  bid_size: number;
  ask_size: number;
  ts_event: number;
}
interface VwapRow {
  instrument_id: string;
  vwap: number;
  ts_event: number;
}
interface TodayRangeRow {
  instrument_id: string;
  high: number;
  low: number;
  prev_close: number;
  ts_event: number;
}
interface FundamentalsRow {
  instrument_id: string;
  market_cap: number | null;
  beta: number | null;
  eps: number | null;
  pe: number | null;
  dividend_amount: number | null;
  as_of: string;
  ts_event: number;
}

// Shared WS planes: one topic per plane, all symbols on it. Framework-private.
const PRICES_TOPIC: Topic = { channel: "prices", params: {} };
const PRICES_KEY = canonicalTopicKey(PRICES_TOPIC);
const QUOTES_TOPIC: Topic = { channel: "quotes", params: {} };
const QUOTES_KEY = canonicalTopicKey(QUOTES_TOPIC);
const VWAPS_TOPIC: Topic = { channel: "vwaps", params: {} };
const VWAPS_KEY = canonicalTopicKey(VWAPS_TOPIC);
const TODAY_RANGES_TOPIC: Topic = { channel: "today_ranges", params: {} };
const TODAY_RANGES_KEY = canonicalTopicKey(TODAY_RANGES_TOPIC);
const FUNDAMENTALS_TOPIC: Topic = { channel: "fundamentals", params: {} };
const FUNDAMENTALS_KEY = canonicalTopicKey(FUNDAMENTALS_TOPIC);

/** Select ONE symbol's row off a shared WS plane. The single subscribe/select path for prices + quotes —
 *  returns the row reference (stable between store pushes) so a caller only re-renders on a new snapshot,
 *  not on every symbol's tick. `topic`/`key`/`listKey` are module constants (stable) → empty dep array. */
function useSharedPlaneRow<T extends { instrument_id: string }>(
  topic: Topic,
  key: string,
  listKey: string,
  instrumentId: string,
): T | null {
  const subscribe = useCallback((cb: () => void) => {
    const releaseTopic = wsManager.subscribe(topic);
    const releaseListener = wsManager.onChange(cb);
    return () => {
      releaseListener();
      releaseTopic();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- topic/key are stable module constants
  }, []);
  const getSnapshot = () => {
    const rows = (wsManager.getTopicState(key)?.data as Record<string, T[]> | undefined)?.[listKey];
    return rows?.find((r) => r.instrument_id === instrumentId) ?? null;
  };
  return useSyncExternalStore(subscribe, getSnapshot, () => null);
}

/**
 * The WHOLE plane, not one row — for tiles that hold a LIST of instruments (the book, the portfolio
 * table) and would otherwise need a hook per symbol, which hooks cannot do.
 *
 * Returns the raw array so the identity is the plane's own; callers index it themselves.
 */
function useSharedPlane<T extends { instrument_id: string }>(topic: Topic, key: string, listKey: string): T[] {
  const subscribe = useCallback((cb: () => void) => {
    const releaseTopic = wsManager.subscribe(topic);
    const releaseListener = wsManager.onChange(cb);
    return () => {
      releaseListener();
      releaseTopic();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- topic/key are stable module constants
  }, []);
  const getSnapshot = () =>
    ((wsManager.getTopicState(key)?.data as Record<string, T[]> | undefined)?.[listKey] ?? EMPTY) as T[];
  return useSyncExternalStore(subscribe, getSnapshot, () => EMPTY as T[]);
}

/** Stable empty reference — a fresh [] each call would make useSyncExternalStore loop forever. */
const EMPTY: never[] = [];

/** Prior close per instrument, for day-change across a list of holdings. */
export function useTodayRanges(): Map<string, number> {
  const rows = useSharedPlane<TodayRangeRow>(TODAY_RANGES_TOPIC, TODAY_RANGES_KEY, "today_ranges");
  return useMemo(() => {
    const m = new Map<string, number>();
    // `high: null` is the engine's TOMBSTONE for a symbol whose snapshot went stale — its prev_close
    // must not be trusted either, or a day change would be computed against a prior day's close.
    for (const r of rows) if (r.high != null && r.prev_close != null) m.set(r.instrument_id, r.prev_close);
    return m;
  }, [rows]);
}

function useLivePrice(instrumentId: string): number | null {
  return useSharedPlaneRow<LivePriceRow>(PRICES_TOPIC, PRICES_KEY, "prices", instrumentId)?.price ?? null;
}

function useLiveQuote(instrumentId: string): QuoteRow | null {
  return useSharedPlaneRow<QuoteRow>(QUOTES_TOPIC, QUOTES_KEY, "quotes", instrumentId);
}

function useLiveVwap(instrumentId: string): number | null {
  return useSharedPlaneRow<VwapRow>(VWAPS_TOPIC, VWAPS_KEY, "vwaps", instrumentId)?.vwap ?? null;
}

function useLiveTodayRange(instrumentId: string): TodayRangeRow | null {
  return useSharedPlaneRow<TodayRangeRow>(TODAY_RANGES_TOPIC, TODAY_RANGES_KEY, "today_ranges", instrumentId);
}

function useLiveFundamentals(instrumentId: string): FundamentalsRow | null {
  return useSharedPlaneRow<FundamentalsRow>(FUNDAMENTALS_TOPIC, FUNDAMENTALS_KEY, "fundamentals", instrumentId);
}

export interface Quote {
  bid: number;
  ask: number;
  mid: number;
  spread: number;
  /** NBBO depth at the touch — already flowed through the `quotes` WS plane since Alpaca's `bs`/`as`
   *  fields were first parsed (#40's exec-side quote), just never exposed here until now (#182 follow-up:
   *  watchlist KPI config). */
  bidSize: number;
  askSize: number;
}

export interface TodayRange {
  high: number;
  low: number;
  prevClose: number;
}

export interface Fundamentals {
  marketCap: number | null;
  beta: number | null;
  eps: number | null;
  pe: number | null;
  dividendAmount: number | null;
}

export interface InstrumentData {
  /** OHLC bars for the chart (chart granularity), ascending by ts. */
  bars: BarDTO[];
  /** Mark price: real-time trade if one has arrived, else the last bar close. Null until any data. */
  price: number | null;
  /** Latest NBBO bid/ask + derived mid/spread (#40). Null until a quote arrives. */
  quote: Quote | null;
  /** Session VWAP, RTH-only (#182 follow-up). Null until real volume has accumulated this session — see
   *  the backend's `_update_vwap_from_bar`. */
  vwap: number | null;
  /** Today's high/low + prior session close (#182 follow-up, KPI Phase 3), refreshed ~5s. Null until the
   *  engine's first successful Alpaca snapshot batch. */
  todayRange: TodayRange | null;
  /** Market Cap/Beta/EPS/trailing P/E/Dividend Amount (#182 follow-up, KPI Phase 2), refreshed ~6h. Null
   *  until the engine's first successful FMP fetch, then persists through transient failures (no
   *  tombstone — fundamentals have no daily-boundary reset). */
  fundamentals: Fundamentals | null;
  /** This symbol's fills (entry/exit markers), filtered off the shared fills plane. */
  fills: FillDTO[];
  /** Resolution status of the bars stream (loading | live | stale | error). */
  status: SourceStatus;
}

/** Bars ONLY, for a caller that just needs a second/third granularity's candles (e.g. a multi-timeframe
 *  trend/cloud read) and doesn't want to also pull that call's own price/quote/fills subscriptions —
 *  `useInstrument` fetches those every time, which is redundant work when only `bars` is needed (code
 *  review, #179). Still the SAME underlying `useSource("bars", …)` — same cache, same WS topic. */
export function useBars(instrumentId: string, granularity: string): BarDTO[] {
  const { data } = useSource("bars", { symbol: instrumentId, granularity });
  return (data as { bars?: BarDTO[] } | undefined)?.bars ?? [];
}

export function useInstrument(instrumentId: string, granularity: string = "1d"): InstrumentData {
  const { data: barsData, status } = useSource("bars", { symbol: instrumentId, granularity });
  const bars = (barsData as { bars?: BarDTO[] } | undefined)?.bars ?? [];
  const { data: fillsData } = useSource("fills", { symbol: instrumentId });
  const allFills = (fillsData as { fills?: FillDTO[] } | undefined)?.fills ?? [];
  const fills = allFills.filter((f) => f.instrument_id === instrumentId);

  const live = useLivePrice(instrumentId);
  const price = live ?? (bars.length ? bars[bars.length - 1].close : null);

  const q = useLiveQuote(instrumentId);
  const quote: Quote | null = q
    ? { bid: q.bid, ask: q.ask, mid: (q.bid + q.ask) / 2, spread: q.ask - q.bid, bidSize: q.bid_size, askSize: q.ask_size }
    : null;

  const vwap = useLiveVwap(instrumentId);

  const tr = useLiveTodayRange(instrumentId);
  const todayRange: TodayRange | null = tr ? { high: tr.high, low: tr.low, prevClose: tr.prev_close } : null;

  const fd = useLiveFundamentals(instrumentId);
  const fundamentals: Fundamentals | null = fd
    ? { marketCap: fd.market_cap, beta: fd.beta, eps: fd.eps, pe: fd.pe, dividendAmount: fd.dividend_amount }
    : null;

  return { bars, price, quote, vwap, todayRange, fundamentals, fills, status };
}

export interface Pnl {
  /** Percent gain/loss, sign-adjusted for side. */
  pct: number;
  /** Unrealized amount (price − entry) × qty, sign-adjusted; null until a price is known. */
  amt: number | null;
}

/** Position P&L from a position + a mark price. Position-scoped (not on `useInstrument`) — one symbol can
 *  hold multiple positions. The ONE place mark-to-market is computed. */
export function computePnl(position: PositionDTO, price: number | null): Pnl {
  if (price == null) return { pct: 0, amt: null };
  // BOTH HALVES POINT THE SAME WAY BECAUSE BOTH READ ONE PREDICATE (#855). The amount and the percent
  // used to apply the side independently — `quantity * (long ? 1 : -1)` and `long ? raw : -raw`, four
  // lines apart — which is two derivations of one fact, and they could disagree. They did: handed a
  // signed quantity the amount negated twice and came out positive while the percent stayed negative,
  // so the headline read a gain beside a loss on the same position.
  const signed = signedQty(position.side, position.quantity);
  // A zero entry price divides to Infinity and renders as "Infinity%". It is reachable: a
  // reconciled position whose opening fill the cache never saw, or a broker report with the field
  // absent. The amount is still meaningful without it — only the percentage needs a basis — so
  // report the amount and leave the percentage unknown rather than printing a number that is not
  // one.
  const basis = position.avg_px_open;
  // A QUANTITY WE CANNOT READ IS UNKNOWN, NOT ZERO. `magnitude` maps a non-finite quantity to 0 so the
  // sign rule stays total — it runs on render paths where a raise blanks the screen — but 0 dollars is
  // a confident answer and this field has an em-dash for "we do not know". Before the predicate, a
  // non-finite quantity produced NaN here and the `Number.isFinite` guard below turned it into null;
  // the guard is kept, and the unknown is now recognised at the input instead of relied on to
  // propagate. A genuine 0 quantity is still a genuine 0 amount.
  const amt = Number.isFinite(position.quantity) ? (price - basis) * signed : Number.NaN;
  if (!basis || !Number.isFinite(basis)) return { pct: 0, amt: Number.isFinite(amt) ? amt : null };
  // The percent keeps its own shape — ±((price − basis) / basis) — because it is a RETURN ON THE
  // BASIS, not a quantity: scaling it by the size would make a 10-share and a 100-share position at
  // the same entry report different percentages. Only the DIRECTION comes from the predicate, and it
  // is the same direction the amount above used.
  const raw = ((price - basis) / basis) * 100;
  return { pct: raw * sideSign(position.side), amt: Number.isFinite(amt) ? amt : null };
}
