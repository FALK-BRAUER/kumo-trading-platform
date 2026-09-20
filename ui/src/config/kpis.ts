/**
 * KPI composition root — registers named watchlist-row stats. Imported for side effect by the board
 * (alongside `./tiles`/`./datasources`/`./checklists`). Mirrors `./checklists.ts`'s role: the generic
 * `kpi/` framework stays stat-agnostic; this file is where a specific stat's formatting logic lives.
 *
 * Phase 1 (#182 follow-up) — the two KPIs needing zero new backend work: volume is already on `BarDTO`,
 * bid/ask size already flows through the `quotes` WS plane (widened onto `Quote` this same phase).
 */
import { registerKpi } from "@/lib/framework/kpi/registry";

function formatCompact(n: number): string {
  // T/B tiers matter for market_cap specifically — without them a mega-cap prints as "4537071.0M"
  // (12 chars) instead of "4.5T" (4 chars), which alone was blowing the KPI row past one line.
  if (n >= 1_000_000_000_000) return `${(n / 1_000_000_000_000).toFixed(1)}T`;
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toFixed(0);
}

registerKpi({
  id: "volume",
  label: "Volume",
  shortLabel: "Vol",
  sortValue: (ctx) => ctx.bars.at(-1)?.volume ?? null,
  format: (ctx) => {
    const last = ctx.bars.at(-1);
    return last ? formatCompact(last.volume) : null;
  },
});

registerKpi({
  id: "bid_ask_size",
  label: "Bid/Ask size",
  shortLabel: "B/A",
  // No natural single-number sort target (two sizes, not one stat) — sortValue omitted, matches the
  // optional contract (code review, #182 follow-up: not every KPI needs to be sortable).
  format: (ctx) => {
    if (!ctx.quote) return null;
    return `${formatCompact(ctx.quote.bidSize)} x ${formatCompact(ctx.quote.askSize)}`;
  },
});

// Phase 2 (#182 follow-up): session VWAP, RTH-only, backed by the `vwaps` WS plane. Null until the
// backend has accumulated real volume this session (extended-hours-only symbols, or the first seconds
// after the open) — format() correctly shows the row's placeholder rather than a stale/zero value.
registerKpi({
  id: "vwap",
  label: "VWAP",
  shortLabel: "VWAP",
  sortValue: (ctx) => ctx.vwap,
  format: (ctx) => (ctx.vwap == null ? null : ctx.vwap.toFixed(2)),
});

// Phase 3 (#182 follow-up): today's high-low band + prior session close, both from the same Alpaca batch
// snapshot (`today_ranges` WS plane) — no separate backend plumbing per KPI, just two views of one DTO.
registerKpi({
  id: "today_range",
  label: "Today's range",
  shortLabel: "Rng",
  // No single natural sort value (a band, not one number) — sortValue omitted, same as bid_ask_size.
  format: (ctx) => (ctx.todayRange == null ? null : `${ctx.todayRange.low.toFixed(2)}-${ctx.todayRange.high.toFixed(2)}`),
});

registerKpi({
  id: "prior_close",
  label: "Prior close",
  shortLabel: "PC",
  sortValue: (ctx) => ctx.todayRange?.prevClose ?? null,
  format: (ctx) => (ctx.todayRange == null ? null : ctx.todayRange.prevClose.toFixed(2)),
});

// Fundamentals (#182 follow-up) — Market Cap/Beta/EPS/trailing P/E/Dividend Amount, all from the same
// `fundamentals` WS plane (one FMP fetch per symbol backs all five). Forward P/E and next Dividend Date
// are deliberately NOT registered here — deferred pending a confirmed clean FMP field for each (see the
// metric contract), not silently dropped.
registerKpi({
  id: "market_cap",
  label: "Market cap",
  shortLabel: "MCap",
  sortValue: (ctx) => ctx.fundamentals?.marketCap ?? null,
  format: (ctx) => {
    const v = ctx.fundamentals?.marketCap;
    return v == null ? null : formatCompact(v);
  },
});

registerKpi({
  id: "beta",
  label: "Beta",
  shortLabel: "β",
  sortValue: (ctx) => ctx.fundamentals?.beta ?? null,
  format: (ctx) => {
    const v = ctx.fundamentals?.beta;
    return v == null ? null : v.toFixed(2);
  },
});

registerKpi({
  id: "eps",
  label: "EPS (trailing annual)",
  shortLabel: "EPS",
  sortValue: (ctx) => ctx.fundamentals?.eps ?? null,
  format: (ctx) => {
    const v = ctx.fundamentals?.eps;
    return v == null ? null : v.toFixed(2);
  },
});

registerKpi({
  id: "pe",
  label: "P/E (trailing)",
  shortLabel: "P/E",
  sortValue: (ctx) => ctx.fundamentals?.pe ?? null,
  format: (ctx) => {
    const v = ctx.fundamentals?.pe;
    return v == null ? null : v.toFixed(1);
  },
});

registerKpi({
  id: "dividend_amount",
  label: "Dividend (last paid)",
  shortLabel: "Div",
  sortValue: (ctx) => ctx.fundamentals?.dividendAmount ?? null,
  format: (ctx) => {
    const v = ctx.fundamentals?.dividendAmount;
    return v == null ? null : v.toFixed(2);
  },
});
