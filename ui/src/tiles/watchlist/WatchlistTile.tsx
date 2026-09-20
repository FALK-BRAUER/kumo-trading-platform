"use client";

/**
 * WatchlistTile (#114) — dense, terminal-style list matching the design mock: a symbol-first table where
 * each entry is a MAIN row (symbol · signal · last · chg% · trend) + a full-width SUB line (cloud position
 * · TK/KJ/Cld). Replaces the old MasterCard card layout. Symbol column is frozen (sticky) so identity stays
 * on horizontal scroll; row tap → the symbol detail surface; remove ✕ is touch-reachable.
 *
 * One `WatchRow` per symbol so each gets its own `useInstrument` (bars + live price) — the rules-of-hooks
 * safe pattern for a dynamic instrument count. Render-only, no polling.
 */
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { canSwipeToDelete } from "./swipeGuard";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Settings, X } from "lucide-react";
import { lastLevels, recommend, TONE_PILL, cloudPosition, type CloudPosition, type Levels } from "@/lib/ichimoku";
import { getWatchlist, removeFromWatchlist } from "@/lib/api/client";
import { TileState } from "@/components/board/TileState";
import { Sparkline } from "@/components/ds/Sparkline";
import { TrendStrip } from "@/components/ds/TrendStrip";
import { marketSession, type MarketSession } from "@/lib/market";
import { priceState } from "@/lib/priceState";
import { buildTrendWindows } from "@/components/ds/trend";
import { DataTable, DataRow, type DataColumn } from "@/components/ds/DataTable";
import { useInstrument, useBars } from "@/lib/framework/instrument";
import { useCockpitStore } from "@/lib/framework/store";
import { focusFromInstrumentId } from "@/lib/framework/focus";
import { getChecklist } from "@/lib/framework/checklist/registry";
import { evaluateChecklist, type ChecklistResult } from "@/lib/framework/checklist/types";
import { getKpi, allKpis } from "@/lib/framework/kpi/registry";
import type { KpiContext } from "@/lib/framework/kpi/types";
import type { TileProps } from "@/lib/framework/types";
import { WATCHLIST_STATUS_FILTERS, WATCHLIST_TIER_FILTERS, WATCHLIST_CLOUD_FILTERS, type WatchlistConfig } from "./schema";
import { DEFAULT_WATCHLIST_KPIS } from "./defaults";
import { reportRowState, clearRowState, subscribeRowState, getRowStateSnapshot } from "./rowState";
import { readPersistedConfig, writePersistedConfig } from "./persistence";

/** Sortable base row fields — NOT registered KPIs (they're always-visible core columns, not toggle-able
 *  display stats), but the sort-by dropdown treats them uniformly alongside KPI ids so "sort by Chg%" and
 *  "sort by Volume" are the same mechanism, not two different code paths. */
const BASE_SORT_FIELDS: { id: string; label: string }[] = [
  { id: "symbol", label: "Symbol" },
  { id: "last", label: "Last" },
  { id: "chg_pct", label: "Chg%" },
];

function sortOptions(): { id: string; label: string }[] {
  return [...BASE_SORT_FIELDS, ...allKpis().filter((k) => k.sortValue).map((k) => ({ id: k.id, label: k.label }))];
}

const CLOUD_CHIP_GLYPH: Record<CloudPosition, string> = { above: "▲", in: "●", below: "▼" };
const CLOUD_CHIP_CLS: Record<CloudPosition, string> = {
  above: "text-status-bull",
  in: "text-status-watch",
  below: "text-status-bear",
};

/** Cloud position at ONE granularity, rendered as a tiny coloured chip (▲ above / ● in / ▼ below) — via
 *  the SAME `cloudPosition()` comparison the main cloud line uses, so they can't disagree at the boundary
 *  (code review, #179). Null levels/price (loading, or fewer than 52 bars — e.g. a symbol just added)
 *  render muted, never crash. */
function cloudChip(label: string, price: number | null, levels: Levels | null) {
  const pos = cloudPosition(price, levels);
  if (pos == null) {
    return (
      <span key={label} className="text-t3">
        {label}·
      </span>
    );
  }
  return (
    <span key={label} className={CLOUD_CHIP_CLS[pos]}>
      {label}
      {CLOUD_CHIP_GLYPH[pos]}
    </span>
  );
}

const TIER_CLS: Record<string, string> = {
  "+++": "text-status-bull",
  "++": "text-status-bull",
  "+": "text-status-watch",
  "=": "text-status-watch",
  "--": "text-status-bear",
  "---": "text-status-bear",
  "?": "text-t3", // insufficient history — NOT a bearish score (code review, #181)
};

/** ledger-provider's 8-condition Blue Flag checklist (#181) — the registered "blue_flag"
 *  config, not hardcoded here; a different methodology would render identically via a different
 *  registered id. Just the tier badge (++/--/etc.) under the symbol, alongside the 3 cloud chips — the
 *  8 individual condition marks were tried on the sub-line and dropped (2026-07-31: too much
 *  detail for the row; the tier badge alone is the at-a-glance signal). */
function blueFlagTierBadge(result: ChecklistResult | null) {
  if (!result) return null;
  return (
    <span className={`font-mono font-semibold ${TIER_CLS[result.tier] ?? "text-t3"}`} title={result.veto ?? undefined}>
      {result.tier}
    </span>
  );
}

/** Configured KPI values (#182 follow-up) — `getKpi` is looked up defensively (no `!`): an unknown or
 *  misconfigured id is skipped, never a crash (code review). Renders as ONE dot-joined line — same
 *  convention as `levelsStr`'s "TK 260.35 · KJ 258.06 · Cld 259.21" — living in the SAME right-side area
 *  (Operator: the left column is reserved for the Blue Flag tier + cloud chips, not KPIs; "most of the kpis
 *  go where TK/KJ/Cld are"). VWAP is excluded here — it gets its own main-row column instead
 *  (`WATCH_COLS`/`cells`), since the operator called it out as deserving "a true column". */
function kpiInlineRow(kpiIds: string[], ctx: KpiContext) {
  const parts = [...new Set(kpiIds)] // dedupe — a repeated id (bad config/localStorage value) would
    // otherwise render twice (code review, #182 follow-up)
    .filter((id) => id !== "vwap") // has its own column — see WatchRow's `vwapStr`
    .map((id) => {
      const def = getKpi(id);
      if (!def) return null;
      const value = def.format(ctx);
      return value == null ? null : `${def.shortLabel ?? def.label} ${value}`;
    })
    .filter((s): s is string => s != null);
  return parts.length === 0 ? null : parts.join(" · ");
}

/** `sortBy` resolution — a `BASE_SORT_FIELDS` id reads directly off row state already computed here
 *  (symbol/price/chgPct); anything else defers to the KPI registry's own `sortValue` (code review, #182
 *  follow-up Phase 4: one mechanism, not a special case per base field vs KPI). */
function sortValueFor(
  sortBy: string | undefined,
  symbol: string,
  price: number | null,
  chgPct: number | null,
  kpiCtx: KpiContext,
): number | string | null {
  if (!sortBy) return null;
  if (sortBy === "symbol") return symbol;
  if (sortBy === "last") return price;
  if (sortBy === "chg_pct") return chgPct;
  return getKpi(sortBy)?.sortValue?.(kpiCtx) ?? null;
}

function WatchRow({
  session,
  instrumentId,
  rowKey,
  hidden,
  onRemove,
  kpiIds,
  sortBy,
}: {
  /** Computed once per tile, not per row — one timer, and every row agrees. */
  session: MarketSession;
  instrumentId: string;
  /** Store key for `rowState.ts` — `${instanceId}:${instrumentId}`, NOT just `instrumentId` (that store is
   *  module-level/shared across every mounted tile instance; two Watchlist tiles with different sortBy
   *  would otherwise clobber each other's reported sortValue for a symbol they both show). */
  rowKey: string;
  /** True when this symbol is excluded by the active status filter. The row still mounts and reports its
   *  state (below) so a LATER status change can bring it back into view under the same filter — filtering
   *  it out of the mounted set entirely would freeze its reported status forever (codex review, #182
   *  follow-up Phase 4). Only the visual output is suppressed. */
  hidden: boolean;
  onRemove: () => void;
  kpiIds: string[];
  sortBy: string | undefined;
}) {
  const { bars, price, quote, vwap, todayRange, fundamentals, status } = useInstrument(instrumentId);
  // Multi-timeframe trend + multi-cloud (design proposal, docs/design-system/proposal-multi-trend-cloud.md)
  // — fixed granularity set, same every render, rules-of-hooks safe. `bars`/`price` above stay the
  // default "1d" call (unchanged) and still drive the signal/existing single-Trend column. Bars-only
  // fetch for the other 4 (code review, #179): `useInstrument` also subscribes to that call's own
  // price/quote/fills, which is redundant work when only candles are needed here.
  const m1Bars = useBars(instrumentId, "1m");
  const m15Bars = useBars(instrumentId, "15m");
  const h1Bars = useBars(instrumentId, "1h");
  const w1Bars = useBars(instrumentId, "1w");
  const openDetailForSymbol = useCockpitStore((s) => s.openDetailForSymbol);
  const levels = lastLevels(bars);
  const rec = recommend(levels, 0); // no position → signal from cloud alone
  const symbol = instrumentId.split(".")[0];
  const sigText = TONE_PILL[rec.tone].split(" ")[1] ?? "text-t2";

  const closes = bars.map((b) => b.close);
  // `todayRange.prevClose` (backend/Alpaca-snapshot-derived, refreshed ~5s) is the source of truth — the
  // "PC" KPI column reads the same field. `bars[bars.length-2].close` was a second, independent guess at
  // the same value (assuming the last daily bar is today's still-forming one), and the two can silently
  // disagree since they refresh on different cadences from different planes — this line and the "PC"
  // column could show contradictory numbers for the same instant (bug found via screenshot, code review).
  // Bars-derived value kept ONLY as a fallback until the today_ranges plane has loaded.
  const prevClose = todayRange?.prevClose ?? (closes.length >= 2 ? closes[closes.length - 2] : null);
  // A PRICE WITH NO SESSION CONTEXT IS NOT A PRICE (#355, #356). The percentage used to be computed here
  // from whatever `price` happened to be — which premarket is frequently an OFFER, not a trade. AMGN
  // rendered +1.34% on zero traded shares against a 1-lot bid and a 570-lot ask, and that number feeds
  // the gap-up entry rule. `priceState` decides what the number IS before anything formats it.
  const state = priceState({
    price,
    session,
    todayRange: { ...(todayRange ?? {}), prevClose },
    quote,
  });
  const chgPct = state.pct;
  const chgCls = chgPct == null ? "text-t3" : chgPct > 0 ? "text-status-bull" : chgPct < 0 ? "text-status-bear" : "text-t2";

  // "above/in/below cloud" text label removed — the 1d cloud chip below already says this (#181).
  const cloudPos = cloudPosition(price, levels);

  const levelsStr = levels
    ? [
        levels.tenkan != null ? `TK ${levels.tenkan.toFixed(2)}` : null,
        levels.kijun != null ? `KJ ${levels.kijun.toFixed(2)}` : null,
        levels.cloudTop != null ? `Cld ${levels.cloudTop.toFixed(2)}` : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : "";
  const placeholder = status === "error" ? "no feed" : "loading…";

  const kpiCtx: KpiContext = { instrumentId, price, quote, bars, vwap, todayRange, fundamentals };
  const vwapStr = getKpi("vwap")?.format(kpiCtx); // its own main-row column, not part of kpiInlineRow
  const kpiInlineStr = kpiInlineRow(kpiIds, kpiCtx);

  // Moved above the report effect below (was computed further down, alongside `trendWindows`) — the
  // effect's dep array needs `tier`, and hook-call ORDER is what rules-of-hooks actually constrains, not
  // where a plain `const` gets computed; `evaluateChecklist` isn't a hook.
  const blueFlagDef = getChecklist("blue_flag");
  const blueFlagResult = blueFlagDef ? evaluateChecklist(blueFlagDef, { weeklyBars: w1Bars, dailyBars: bars }) : null;
  const tier = blueFlagResult?.tier ?? null;

  // Report this row's sort/filter-relevant state up to the parent (#182 follow-up, Phase 4) — see
  // rowState.ts's docstring for why this can't just be lifted into the parent directly (rules-of-hooks
  // for a dynamic instrument count). Effect (not inline) so a report never happens mid-render. `tier`
  // (Blue Flag) and `cloudPos` (1d cloud position) ride along here too (Phase 5: tier + cloud-position
  // filters) — same mechanism as `status`/`sortValue`, not a new reporting path.
  const sortValue = sortValueFor(sortBy, symbol, price, chgPct, kpiCtx);
  useEffect(() => {
    reportRowState(rowKey, { symbol, status: rec.status, sortValue, tier, cloudPos });
  }, [rowKey, symbol, rec.status, sortValue, tier, cloudPos]);

  // Unmount cleanup — fires for EVERY unmount reason (this tile's own remove, an external refetch that
  // drops the symbol, a query-cache update from elsewhere), not just this tile's remove-mutation path
  // (codex review, #182 follow-up Phase 4). Separate effect, `[rowKey]`-only deps, so it does NOT fire on
  // every state re-report above — only on true mount/unmount of this row identity.
  useEffect(() => () => clearRowState(rowKey), [rowKey]);

  // 6-window trend strip — count-based slicing (matches the existing single-Trend column's
  // `closes.slice(-24)` convention), each window pulled from whichever granularity is the smallest bar
  // whose historical range covers it (see proposal doc for the mapping + why). Pure slicing policy lives
  // in `buildTrendWindows` (tested independently, #179 code review).
  const levels15m = lastLevels(m15Bars);
  const levels1h = lastLevels(h1Bars);
  // Each granularity's own last close, NOT the "1d"-derived `price` above — comparing 15m/1h cloud levels
  // against a daily close was the code-review finding: self-consistent within each granularity avoids
  // that mismatch without re-subscribing to the live-price plane again per granularity.
  const price15m = m15Bars.length ? m15Bars[m15Bars.length - 1].close : null;
  const price1h = h1Bars.length ? h1Bars[h1Bars.length - 1].close : null;
  // Time-sliced, not count-sliced: a count window slides its own baseline while history streams in, so
  // 1Y/5Y kept changing with an unchanged last price. Carries ts_event so the window can say whether it
  // is actually covered.
  const toTrendBars = (bs: typeof bars) => bs.map((b) => ({ ts: b.ts_event, close: b.close }));
  const trendWindows = buildTrendWindows({
    m1: toTrendBars(m1Bars),
    h1: toTrendBars(h1Bars),
    d1: toTrendBars(bars),
    w1: toTrendBars(w1Bars),
  });

  const open = () => openDetailForSymbol(focusFromInstrumentId(instrumentId, { tab: "watch" }, symbol));

  // Slide-to-delete (#64): the row follows the finger left; release past the threshold removes the symbol,
  // else it snaps back. ONE gesture, no confirm tap. Horizontal-dominant drag only, so vertical page-scroll
  // and the row's tap-to-open are untouched. Desktop (hover-capable) keeps the ✕ button instead.
  //
  // #294 — SCROLLING RIGHT AND DELETING WERE THE SAME GESTURE. `DataTable` wraps rows in
  // `overflow-x-auto` and the watchlist is wider than a phone (#250), so revealing the right-hand
  // columns means dragging the finger LEFT — horizontal-dominant, negative ddx, which is exactly what
  // armed the delete. Reading a price column could remove the symbol, and the two are indistinguishable
  // from the touch deltas alone because they ARE the same deltas.
  //
  // The discriminator is the scroll container, not the gesture: a delete may only start when the row is
  // already scrolled fully left, so there is nothing further left to reveal. That is the iOS convention
  // and it needs no threshold tuning — while any rightward content remains hidden the drag is a scroll,
  // and once the user is at the left edge a leftward drag can only mean delete.
  const COMMIT = 96; // px past which release deletes
  const [dx, setDx] = useState(0);
  const drag = useRef<{ x: number; y: number; swiping: boolean } | null>(null);
  const justSwiped = useRef(false);
  const onTouchStart = (e: React.TouchEvent) => {
    const t = e.touches[0];
    drag.current = { x: t.clientX, y: t.clientY, swiping: false };
    justSwiped.current = false;
  };
  const onTouchMove = (e: React.TouchEvent) => {
    const d = drag.current;
    if (!d) return;
    const t = e.touches[0];
    const ddx = t.clientX - d.x;
    const ddy = t.clientY - d.y;
    if (!d.swiping && ddx < 0 && Math.abs(ddx) > Math.abs(ddy) + 6 && canSwipeToDelete(e.currentTarget))
      d.swiping = true;
    if (d.swiping) setDx(Math.max(-140, Math.min(0, ddx)));
  };
  const onTouchEnd = () => {
    const d = drag.current;
    drag.current = null;
    if (d?.swiping) {
      justSwiped.current = true; // swallow the click that follows a swipe (don't open the detail)
      if (dx <= -COMMIT) { onRemove(); return; }
    }
    setDx(0);
  };
  const rowClick = () => { if (justSwiped.current) { justSwiped.current = false; return; } open(); };
  const armed = dx <= -COMMIT;

  // Hooks above have all already run unconditionally (rules-of-hooks safe) — this filters only the VISUAL
  // output. Keeps the row mounted (and reporting) even while filtered out, see the `hidden` prop's doc.
  if (hidden) return null;

  return (
    <DataRow
      columns={WATCH_COLS}
      onClick={rowClick}
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
      onTouchEnd={onTouchEnd}
      swipeX={dx}
      className={armed ? "bg-status-bear/20" : dx < 0 ? "bg-status-bear/10" : undefined}
      cells={[
        <span key="s" className="text-[13px] font-bold text-t1">{symbol}</span>,
        <span key="g" className={`text-[10px] font-semibold ${sigText}`}>{rec.status}</span>,
        <span key="v" className="text-[11px] text-t2">{vwapStr ?? "—"}</span>,
        <span key="c" className={chgCls}>{chgPct == null ? "—" : `${chgPct > 0 ? "+" : ""}${chgPct.toFixed(2)}%`}</span>,
        // R3: a quote-only price is a RANGE, not a number. Printing one side of a two-sided market as
        // "the price" is the entire defect — the 570-lot ask became a +1.34% move.
        <span key="l" className="flex flex-col items-end leading-tight">
          {state.kind === "quote" ? (
            <span className="font-mono text-[11px] text-t1">
              {state.bid!.toFixed(2)}–{state.ask!.toFixed(2)}
            </span>
          ) : (
            <span className="text-[13px] text-t1">{state.price != null ? state.price.toFixed(2) : "—"}</span>
          )}
          {/* R2: provenance in TEXT at every breakpoint — colour never carries it. Suppressed only for a
              plain regular-hours trade, where "Traded" on every row is noise rather than information. */}
          {state.kind !== "traded" || state.label !== "Traded" ? (
            <span className="font-mono text-[8px] uppercase tracking-wide text-t3">
              {state.label}
              {state.kind === "quote" && state.spreadPct != null ? ` ${state.spreadPct.toFixed(2)}%` : ""}
            </span>
          ) : null}
        </span>,
        <Sparkline key="t" values={closes.slice(-24)} />,
        <button
          key="x"
          type="button"
          onClick={(e) => { e.stopPropagation(); onRemove(); }}
          aria-label={`Remove ${symbol}`}
          // Desktop affordance only (hover-capable). Touch deletes by sliding the row — no button.
          className="hidden rounded p-2 text-t3 opacity-70 transition-opacity [@media(hover:hover)]:inline-flex hover:bg-ds-surf2 hover:text-t1 hover:opacity-100"
        >
          <X size={14} />
        </button>,
      ]}
      sub={
        <div className="flex items-start justify-between gap-2">
          {/* Blue Flag tier + the 3 cloud-position chips (15m/1h/1d), stacked LEFT — visually under the
              frozen symbol column (2026-07-31), but living on the sub-line `<tr>`, not the symbol's
              own main-row cell: a table row's height is shared across every cell in it, so a 4-line-tall
              symbol cell was stretching the WHOLE main row (Sig/Last/Chg%/Trend cells stayed 1 line,
              leaving a big blank gap in each — the "whitespace" the operator flagged). The sub-line is its own
              row; this costs nothing there. */}
          <div className="flex shrink-0 flex-col gap-0.5 text-[9px] font-mono">
            {blueFlagTierBadge(blueFlagResult)}
            {cloudChip("15m", price15m, levels15m)}
            {cloudChip("1h", price1h, levels1h)}
            {cloudChip("1d", price, levels)}
          </div>
          {/* `min-w-0 flex-1` — the column takes the row's remaining width instead of sizing itself to
              whichever child happens to be widest. It had been 204px on a 374px split for no better
              reason than that the levels line measured 204, leaving ~140px unused beside 24px of cloud
              chips. The trend strip is what wanted that width (#294). */}
          <div className="flex min-w-0 flex-1 flex-col items-end gap-0.5 text-right">
            {/* KPIs SHARE THE LEVELS LINE. Volume had a line to itself, which cost a row of vertical
                space to carry one short value — Operator: "the vol does not need own line". Same
                `flex-wrap` container, so a KPI set too wide for the viewport still wraps rather than
                truncating, and on a phone it lands under the levels exactly where it used to be.
                (Operator: "get the 6 kpis in one line" — keep VWAP as its own column, fix the rest.) */}
            <div className="flex flex-wrap items-center justify-end gap-x-2 gap-y-0.5">
              {cloudPos ? (
                levelsStr && <span>{levelsStr}</span>
              ) : (
                <span className="italic">{placeholder}</span>
              )}
              {kpiInlineStr && (
                <span className="whitespace-nowrap text-[9px] font-mono text-t2">{kpiInlineStr}</span>
              )}
            </div>
            <TrendStrip windows={trendWindows} />
          </div>
        </div>
      }
    />
  );
}

const WATCH_COLS: DataColumn[] = [
  { label: "Symbol" },
  { label: "Sig" },
  { label: "VWAP", align: "right" }, // real main-row column (Operator: deserves "a true column", not buried in the sub-line)
  { label: "Chg%", align: "right" },
  { label: "Last", align: "right" }, // right-most price column (Operator: "price should be right")
  { label: "Trend", className: "hidden sm:table-cell" },
  { label: "", align: "right", cellClassName: "px-1 pt-1" }, // tighter ✕ cell (pixel-parity w/ pre-primitive)
];

// Filter chip options — the statuses `recommend()` can actually return for a WATCHLIST row. Single source
// of truth with the persistence schema is `WATCHLIST_STATUS_FILTERS` (schema.ts): listing anything else
// here would be dead UI that can never match a row, and a schema that accepted anything else would let a
// corrupted localStorage value filter out every row (codex review, #182 follow-up Phase 4).
const STATUS_FILTER_OPTIONS = WATCHLIST_STATUS_FILTERS;
const TIER_FILTER_OPTIONS = WATCHLIST_TIER_FILTERS;
const CLOUD_FILTER_OPTIONS = WATCHLIST_CLOUD_FILTERS;

/** Toggle `value` in/out of a filter array, `undefined` (not `[]`) when the result is empty — matches
 *  every filter field's "empty/omitted → show all" schema contract (schema.ts). Shared by the 3 filter
 *  dimensions (status/tier/cloud, Phase 5) — same toggle shape, not 3 near-duplicate handlers. */
function toggleInArray<T extends string>(arr: readonly T[] | undefined, value: T): T[] | undefined {
  const cur = arr ?? [];
  const next = cur.includes(value) ? cur.filter((v) => v !== value) : [...cur, value];
  return next.length ? next : undefined;
}

/** Gear icon (#182 follow-up, Phase 4/5) — sort/filter/KPI-selection popover. Click-outside-to-close via a
 *  plain document listener (no portal/dedicated primitive exists in this codebase yet for a popover —
 *  matches the lightest-weight pattern that works, not a new abstraction for one caller). */
function GearPopover({ config, onChange }: { config: WatchlistConfig; onChange: (next: WatchlistConfig) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClickOutside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [open]);

  const kpiIds = config.kpis ?? DEFAULT_WATCHLIST_KPIS;
  const toggleKpi = (id: string) => {
    const next = kpiIds.includes(id) ? kpiIds.filter((k) => k !== id) : [...kpiIds, id];
    onChange({ ...config, kpis: next });
  };
  const setSortBy = (id: string) => onChange({ ...config, sortBy: id || undefined });
  const toggleSortDir = () => onChange({ ...config, sortDir: config.sortDir === "desc" ? "asc" : "desc" });
  const statusFilter = config.statusFilter ?? [];
  const toggleStatus = (st: (typeof STATUS_FILTER_OPTIONS)[number]) =>
    onChange({ ...config, statusFilter: toggleInArray(config.statusFilter, st) });
  const tierFilter = config.tierFilter ?? [];
  const toggleTier = (t: (typeof TIER_FILTER_OPTIONS)[number]) =>
    onChange({ ...config, tierFilter: toggleInArray(config.tierFilter, t) });
  const cloudFilter = config.cloudFilter ?? [];
  const toggleCloud = (c: (typeof CLOUD_FILTER_OPTIONS)[number]) =>
    onChange({ ...config, cloudFilter: toggleInArray(config.cloudFilter, c) });

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label="Watchlist settings"
        aria-expanded={open}
        className="rounded p-1.5 text-t3 transition-colors hover:bg-ds-surf2 hover:text-t1"
      >
        <Settings size={14} />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-20 mt-1 w-64 rounded border border-ds-line bg-ds-surf p-3 text-[11px] shadow-lg">
          <div className="mb-1 font-semibold uppercase tracking-wider text-t3">Sort by</div>
          <div className="mb-3 flex items-center gap-1.5">
            <select
              className="flex-1 rounded border border-ds-line bg-ds-surf2 px-1.5 py-1 text-t1"
              value={config.sortBy ?? ""}
              onChange={(e) => setSortBy(e.target.value)}
            >
              <option value="">None</option>
              {sortOptions().map((o) => (
                <option key={o.id} value={o.id}>{o.label}</option>
              ))}
            </select>
            <button
              type="button"
              onClick={toggleSortDir}
              disabled={!config.sortBy}
              aria-label="Toggle sort direction"
              className="rounded border border-ds-line px-2 py-1 text-t2 disabled:opacity-40"
            >
              {config.sortDir === "desc" ? "↓" : "↑"}
            </button>
          </div>

          <div className="mb-1 font-semibold uppercase tracking-wider text-t3">Filter by signal</div>
          <div className="mb-3 flex flex-wrap gap-1">
            {STATUS_FILTER_OPTIONS.map((st) => (
              <button
                key={st}
                type="button"
                onClick={() => toggleStatus(st)}
                className={`rounded px-1.5 py-0.5 font-mono ${
                  statusFilter.includes(st) ? "bg-status-info/20 text-status-info" : "bg-ds-surf2 text-t3"
                }`}
              >
                {st}
              </button>
            ))}
          </div>

          <div className="mb-1 font-semibold uppercase tracking-wider text-t3">Filter by Blue Flag tier</div>
          <div className="mb-3 flex flex-wrap gap-1">
            {TIER_FILTER_OPTIONS.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => toggleTier(t)}
                className={`rounded px-1.5 py-0.5 font-mono font-semibold ${
                  tierFilter.includes(t) ? "bg-status-info/20" : "bg-ds-surf2 text-t3"
                } ${tierFilter.includes(t) ? (TIER_CLS[t] ?? "text-status-info") : ""}`}
              >
                {t}
              </button>
            ))}
          </div>

          <div className="mb-1 font-semibold uppercase tracking-wider text-t3">Filter by cloud position (1d)</div>
          <div className="mb-3 flex flex-wrap gap-1">
            {CLOUD_FILTER_OPTIONS.map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => toggleCloud(c)}
                className={`rounded px-1.5 py-0.5 font-mono ${
                  cloudFilter.includes(c) ? `bg-status-info/20 ${CLOUD_CHIP_CLS[c]}` : "bg-ds-surf2 text-t3"
                }`}
              >
                {c} {CLOUD_CHIP_GLYPH[c]}
              </button>
            ))}
          </div>

          <div className="mb-1 font-semibold uppercase tracking-wider text-t3">KPIs shown</div>
          <div className="flex max-h-40 flex-col gap-1 overflow-y-auto">
            {allKpis().map((k) => (
              <label key={k.id} className="flex items-center gap-1.5 text-t2">
                <input type="checkbox" checked={kpiIds.includes(k.id)} onChange={() => toggleKpi(k.id)} />
                {k.label}
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function WatchlistTile({ instanceId, config, onConfigChange }: TileProps<WatchlistConfig>) {
  // ONE CLOCK FOR THE WHOLE TILE (#356). Twenty rows each calling Date.now() would be twenty answers to
  // one question, and a row that only re-renders when its own data ticks would still be showing
  // "Pre-market" after 09:30 on a symbol that has gone quiet — which is precisely the staleness this
  // issue is about. 30s is far finer than the boundaries it has to catch and costs one timer.
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);
  const session = marketSession(nowMs);
  const queryClient = useQueryClient();
  const { data, status } = useQuery({ queryKey: ["watchlist"], queryFn: getWatchlist, staleTime: 5_000 });
  const symbols = data?.symbols ?? [];

  // localStorage override layered over the layout-file default (Operator: "setting file, we have a concept
  // for this" — not Postgres). Read once per render (cheap sync localStorage.getItem); persistence.ts's
  // own docstring covers why this is separate from the store's `onConfigChange` (in-memory only).
  const persisted = readPersistedConfig(instanceId);
  const effectiveConfig = persisted ?? config;
  const kpiIds = effectiveConfig.kpis ?? DEFAULT_WATCHLIST_KPIS;
  const { sortBy, sortDir, statusFilter, tierFilter, cloudFilter } = effectiveConfig;

  const applyConfig = (next: WatchlistConfig) => {
    writePersistedConfig(instanceId, next);
    onConfigChange(next); // updates THIS session's live render immediately, without needing a reload
  };

  const remove = useMutation({
    mutationFn: removeFromWatchlist,
    onSuccess: (res) => {
      queryClient.setQueryData(["watchlist"], res);
      // No manual clearRowState here — the removed symbol's WatchRow unmounts (it drops out of `symbols`
      // below) and its OWN cleanup effect clears its rowState entry. That fires for every removal path
      // (this mutation, an external refetch, another tab's write to the same query cache), not just this
      // one (codex review, #182 follow-up Phase 4).
    },
  });

  // Compound key: rowState.ts is a MODULE-level store shared by every mounted Watchlist tile — plain
  // `instrumentId` would let two tile instances with different sortBy configs clobber each other's
  // reported sortValue for a symbol they both show (codex review, #182 follow-up Phase 4).
  const rowKeyFor = (instrumentId: string) => `${instanceId}:${instrumentId}`;

  const rowStates = useSyncExternalStore(subscribeRowState, getRowStateSnapshot, getRowStateSnapshot);

  // ALL loaded symbols stay mounted regardless of the filter — see WatchRow's `hidden` prop doc. Filtering
  // them out of the mounted set here would freeze a hidden row's reported status forever, since nothing
  // would ever re-render it to report a change that might bring it back into view (codex review, #182
  // follow-up Phase 4).
  //
  // A row must pass EVERY active dimension (AND, not OR) — "WATCH + above cloud" narrows, it doesn't
  // widen. Not-yet-reported (`value == null`) always passes a given dimension — a false negative (row
  // stays visible one dimension too long) is far less jarring than a row vanishing on load then
  // reappearing (Phase 4 codex review reasoning, extended to tier/cloud in Phase 5).
  const passesFilter = (value: string | null | undefined, active: readonly string[] | undefined): boolean =>
    !active || active.length === 0 || value == null || active.includes(value);

  const visibleSet = new Set(
    symbols.filter((s) => {
      const rs = rowStates.get(rowKeyFor(s));
      // `rs?.status` (rec.status: WATCH/HOLD/EXIT/ADD/TRAIL) is wider than the persisted `statusFilter`'s
      // narrowed enum (WATCH/HOLD/EXIT — the only values `recommend()` produces on this pnlPct=0 tile); a
      // status outside that set (ADD/TRAIL, unreachable here) simply never matches, not a bug.
      return (
        passesFilter(rs?.status, statusFilter) &&
        passesFilter(rs?.tier, tierFilter) &&
        passesFilter(rs?.cloudPos, cloudFilter)
      );
    }),
  );
  const sortedSymbols = sortBy
    ? [...symbols].sort((a, b) => {
        const va = rowStates.get(rowKeyFor(a))?.sortValue ?? null;
        const vb = rowStates.get(rowKeyFor(b))?.sortValue ?? null;
        if (va == null && vb == null) return 0;
        if (va == null) return 1; // not-yet-reported (or genuinely null, e.g. no fundamentals yet) sorts last
        if (vb == null) return -1;
        const cmp = va < vb ? -1 : va > vb ? 1 : 0;
        return sortDir === "desc" ? -cmp : cmp;
      })
    : symbols;

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-t2">Watchlist</h2>
        <div className="flex items-center gap-2">
          <span className="font-mono text-xs text-t3">
            {visibleSet.size === symbols.length ? `${symbols.length} symbols` : `${visibleSet.size}/${symbols.length} symbols`}
          </span>
          <GearPopover config={effectiveConfig} onChange={applyConfig} />
        </div>
      </div>

      <TileState
        status={status === "pending" ? "loading" : status === "error" ? "error" : "live"}
        isEmpty={symbols.length === 0}
        emptyLabel="No symbols — add from search ↑"
      >
        <DataTable columns={WATCH_COLS}>
          {sortedSymbols.map((symbol) => (
            <WatchRow
            session={session}
              key={symbol}
              instrumentId={symbol}
              rowKey={rowKeyFor(symbol)}
              hidden={!visibleSet.has(symbol)}
              onRemove={() => remove.mutate(symbol)}
              kpiIds={kpiIds}
              sortBy={sortBy}
            />
          ))}
        </DataTable>
      </TileState>
    </div>
  );
}
