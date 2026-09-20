/**
 * Focus builders (#69) — construct a `FocusedInstrument` (identity + source context) for the detail surface.
 * Kept DTO-neutral: a portfolio row contributes its `strategy_id` context but NEVER position fields, so the
 * surface never depends on `PositionDTO` and can swap to the `TradeCycleDTO` later.
 */
import type { FocusContext, FocusedInstrument } from "./store";

/** "AAPL.XNAS" → { symbol: "AAPL", venue: "XNAS" }. */
export function parseInstrumentId(instrumentId: string): { symbol: string; venue: string } {
  const dot = instrumentId.indexOf(".");
  return dot < 0
    ? { symbol: instrumentId, venue: "" }
    : { symbol: instrumentId.slice(0, dot), venue: instrumentId.slice(dot + 1) };
}

/** Focus from a bare `instrument_id` (watchlist / portfolio rows) plus the source context. */
export function focusFromInstrumentId(
  instrumentId: string,
  context: FocusContext,
  name = "",
): FocusedInstrument {
  const { symbol, venue } = parseInstrumentId(instrumentId);
  return { instrument_id: instrumentId, symbol, name, venue, context };
}
