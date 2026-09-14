/**
 * Cockpit store — the one Zustand store. Holds client view state: the layouts (seeded from the JSON/TS
 * config, NOT a server fetch — there is no runtime layout editor), the active view, and ephemeral
 * per-tile config (e.g. a chart's timeframe). Layout geometry is config-only; edit it in `@/config/layouts`.
 *
 * The live-data plane (WS multiplex cache) is the separate `liveData` plane (datasource/ws-manager).
 */
import { create } from "zustand";
import type { Layout } from "../layout/schema";
import { readUnit, writeUnit, type Unit } from "@/components/ds/unitPersistence";

/** The instrument the cockpit is focused on (a picked symbol-search match). Carries `name` so the detail
 *  surface header shows it without a refetch. Structurally an `InstrumentMatch`. */
/** Where the detail surface was opened FROM — drives its composition (#70/#71). DTO-neutral on purpose:
 *  it carries `strategy_id`/`trade_cycle_id` (identity), NEVER a PositionDTO or qty/side/P&L, so the surface
 *  can swap from the position model to the TradeCycleDTO later without changing this contract. */
export interface FocusContext {
  tab?: string; // "watch" | "portfolio" | "search" | …
  strategy_id?: string;
  trade_cycle_id?: string; // undefined in Phase 1; filled once the managed book (#77) lands
}

export interface FocusedInstrument {
  instrument_id: string;
  symbol: string;
  name: string;
  venue: string;
  context?: FocusContext;
}

/** What the detail surface is focused on — a discriminated union so a list row can open the detail for its
 *  own entity KIND (symbol / order / position), resolved through the DetailRegistry by (kind, strategy). Each
 *  variant carries only the entity's IDENTITY (id), never a snapshot — the detail re-reads live data by id. */
export type Focus =
  | { kind: "symbol"; instrument: FocusedInstrument }
  | { kind: "order"; clientOrderId: string; context?: FocusContext }
  | { kind: "position"; positionKey: string; instrumentId: string; context?: FocusContext }
  // An unattributed broker position (#79). Its own kind, not `position`: it has no strategy and no cycle,
  // and the only thing you can do with it is move it into one — a different screen from a managed position.
  | { kind: "unclaimed"; instrumentId: string; sourceStrategyId: string; side: string; context?: FocusContext };

export type FocusKind = Focus["kind"];

/** Stable identity of a focus — used as the detail surface's React key so a focus change REMOUNTS it. */
export function focusIdentity(focus: Focus): string {
  switch (focus.kind) {
    case "symbol":
      return `symbol:${focus.instrument.instrument_id}`;
    case "order":
      return `order:${focus.clientOrderId}`;
    case "position":
      return `position:${focus.positionKey}`;
    case "unclaimed":
      return `unclaimed:${focus.sourceStrategyId}:${focus.instrumentId}:${focus.side}`;
  }
}

/** The strategy that scopes a focus's detail composition (falls back to "*" when unknown). Normalizes the
 *  Nautilus StrategyId `NAME-tag` → bare `NAME` so it matches how detail variants register (and how the UI
 *  displays strategy) — else a (kind, strategy) variant could never resolve and dev would warn every render. */
export function focusStrategy(focus: Focus): string {
  const ctx = focus.kind === "symbol" ? focus.instrument.context : focus.context;
  return ctx?.strategy_id?.replace(/-\d+$/, "") ?? "*";
}

interface CockpitStore {
  layouts: Layout[];
  activeViewId: string | null;
  /** Focused instrument (set by symbol-search #25) + whether the detail surface (#27) is open. Ephemeral
   *  client state — NOT a layout mutation (layouts stay config-only). `detailOpen` is separate from focus so
   *  closing the surface keeps the focus (and persists across tab switches); re-selecting the same symbol
   *  still reopens because `openDetailForSymbol` sets `detailOpen=true` regardless. */
  focusedInstrument: FocusedInstrument | null;
  /** The active detail focus (any kind). `focusedInstrument` is the symbol-only projection kept for the
   *  existing symbol-path readers (search chip, order ticket); it is null for order/position focus. */
  focus: Focus | null;
  detailOpen: boolean;
  /** The GLOBAL period every flow figure answers for (#233 follow-up).
   *
   * Operator, 2026-08-16: the BOOK tile mixed three time bases with nothing saying so — DEPLOYED/CASH are
   * state and have no period, REALIZED was scoped to today, and the equity chart had its own selector.
   * So "how did I reach 100k from 95k when NET says 1k" had no answer on screen: NET is unrealized on
   * what is HELD, and the rest of the move was realized on positions already closed.
   *
   * One selector drives every flow number, so realized + change in unrealized reconcile to the equity
   * curve by construction rather than by coincidence. STATE figures ignore it — a period would be
   * meaningless on cash.
   */
  period: string;
  setPeriod: (period: string) => void;
  /** The GLOBAL unit every relative-change figure is rendered in (#586; rule in #392).
   *
   * The second axis of the same question the period selector asks. Operator, 2026-09-06: the toggle
   * SWITCHES a field's unit, it never adds a percentage beside a dollar figure — so this changes how
   * a figure reads, never how many figures there are.
   *
   * GLOBAL like `period`, and for the same reason: two surfaces disagreeing about which unit they are
   * in is the three-time-bases problem again, one axis over.
   *
   * PERSISTED per viewer, which `period` is not. A period is a question you ask repeatedly; a unit is
   * a preference you hold, and re-picking it every reload is the kind of friction that makes a control
   * go unused.
   */
  unit: Unit;
  setUnit: (unit: Unit) => void;
  setLayouts: (layouts: Layout[]) => void;
  setActiveView: (id: string) => void;
  /** Focus ANY entity + open its detail surface (resolved through the DetailRegistry). */
  openDetail: (focus: Focus) => void;
  /** Focus an instrument AND open its detail surface. Symbol-path convenience (search / watch / portfolio). */
  openDetailForSymbol: (instrument: FocusedInstrument) => void;
  /** Close the detail surface only — keeps the focus. */
  closeDetail: () => void;
  activeLayout: () => Layout | undefined;
  /** Tile self-edit → replace one placed tile's config in the active view (ephemeral, not persisted). */
  updateTileConfig: (instanceId: string, config: unknown) => void;

  /** Commands the engine is CURRENTLY working on, keyed by `strategy_id:instrument_id` (#269 follow-up).
   *
   * Operator, 2026-08-17, after a flatten that WORKED: "there was no proper feedback on flatten. The stock
   * looked unchanged."
   *
   * He was right, and the exit had genuinely become slower: it now cancels the resting stop, waits for
   * the venue to CONFIRM, waits for Alpaca to release the reserved shares, and only then closes. His NBIS
   * flatten took 11 seconds end to end and the screen said nothing for all of it — a destructive control
   * that looks inert is one an operator presses twice.
   *
   * Global rather than local to the detail surface for the same reason: the position ROW is what he was
   * watching, and it lives in a different component tree from the slide control he pressed. Local state
   * cannot reach it. */
  busyCommands: Record<string, BusyCommand>;
  /** Mark a position as having work in flight. `startedAt` is passed in rather than read from the clock
   *  here so tests can state the time instead of racing it. */
  beginCommand: (key: string, verb: string, label: string, startedAt: number) => void;
  /** Clear it — on accept, on reject, and on the ack giving up. Every exit path must call this, or the
   *  row stays PROCESSING forever and the next real one is invisible against it. */
  endCommand: (key: string) => void;
}

/** One in-flight destructive command, as the banner and the position row both need to read it. */
export interface BusyCommand {
  /** Machine-readable: `flatten`, `peak`, … Used for the row badge. */
  verb: string;
  /** Human-readable, already including the symbol: "Flattening NBIS". */
  label: string;
  startedAt: number;
}

export const useCockpitStore = create<CockpitStore>((set, get) => ({
  layouts: [],
  activeViewId: null,
  focusedInstrument: null,
  focus: null,
  detailOpen: false,
  //: 1D by default. The day is the window an operator actually acts on, and it is the one the old
  //: per-session REALIZED already answered — so the default preserves today's meaning while making it
  //: selectable rather than implicit.
  period: "1D",
  //: `$` unless this viewer has said otherwise. Every figure was a dollar figure before the toggle
  //: existed, so the default is what the panel already meant.
  unit: readUnit(),
  busyCommands: {},
  beginCommand: (key, verb, label, startedAt) =>
    set((s) => ({ busyCommands: { ...s.busyCommands, [key]: { verb, label, startedAt } } })),
  endCommand: (key) =>
    set((s) => {
      if (!(key in s.busyCommands)) return s;
      const next = { ...s.busyCommands };
      delete next[key];
      return { busyCommands: next };
    }),
  setPeriod: (period) => set({ period }),
  setUnit: (unit) => {
    // WRITE THROUGH, not on a subscription. A persistence effect elsewhere would make the store the
    // source of truth for a value the store did not load, and the write would be one render late.
    writeUnit(unit);
    set({ unit });
  },
  setLayouts: (layouts) =>
    set((s) => ({ layouts, activeViewId: s.activeViewId ?? layouts[0]?.id ?? null })),
  // Switching view CLOSES the detail. The detail surface renders over the tile grid, so leaving it open
  // meant tapping a tab changed the view behind it and looked like the tab was dead (reported from mobile).
  // Focus itself is kept — it also drives the search chip and order ticket, which a navigation shouldn't reset.
  setActiveView: (activeViewId) => set({ activeViewId, detailOpen: false }),
  openDetail: (focus) =>
    set((s) => ({
      focus,
      // Symbol focus updates the projection; order/position focus PRESERVES the last symbol (the detail
      // never reads it, and nulling it would wipe the user's search-chip / order-ticket symbol selection).
      focusedInstrument: focus.kind === "symbol" ? focus.instrument : s.focusedInstrument,
      detailOpen: true,
    })),
  openDetailForSymbol: (instrument) => get().openDetail({ kind: "symbol", instrument }),
  closeDetail: () => set({ detailOpen: false }),
  activeLayout: () => {
    const s = get();
    return s.layouts.find((l) => l.id === s.activeViewId);
  },
  updateTileConfig: (instanceId, config) =>
    set((s) => ({
      layouts: s.layouts.map((l) =>
        l.id !== s.activeViewId
          ? l
          : { ...l, tiles: l.tiles.map((t) => (t.instanceId === instanceId ? { ...t, config } : t)) },
      ),
    })),
}));
