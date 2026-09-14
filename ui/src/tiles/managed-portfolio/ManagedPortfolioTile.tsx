"use client";

import { useState } from "react";

/**
 * ManagedPortfolioTile (#77, migrated to the DataTable primitive #114) — the managed book, not a flat
 * position list. Consumes the trade-cycle plane (`trades` DataSource, #73): a NET-ANCHOR row per instrument.
 * A cycle's lifetime = open→closed spanning flats, so a qty-0 ARMED name STAYS visible (waiting to re-buy) —
 * position ≠ engagement.
 *
 * Uses the shared list contract (same as Watchlist / Orders): a MAIN row whose cells align to the column
 * headers + an always-visible free-form SUB-LINE carrying the per-strategy cycle breakdown. NOT an expander,
 * NOT header-aligned. Row tap → the symbol detail surface.
 *
 * Dual accounting lens (first-class DTO flags, never re-derived here): `is_capital_deployed` (HELD with a live
 * position) drives net-liq / %-deployed; `is_engaged` (any non-terminal cycle) drives the managed-names list.
 * An ARMED qty-0 row is engaged but NOT deployed — it must not inflate %-deployed.
 *
 * The net anchor is the NATIVE net (Σ this node's cycles) — labelled as such: in the ADR the broker net is the
 * hard reconciliation anchor, and per-strategy splits are unverified. A true broker-net DTO + discrepancy
 * indicator is a later add. Cycle P&L is restart-safe (#74), so this is authoritative, not diagnostic.
 */
import { DataTable, DataRow, type DataColumn } from "@/components/ds/DataTable";
import { TileState } from "@/components/board/TileState";
import { useCockpitStore } from "@/lib/framework/store";
import { PERIODS } from "@/components/ds/periods";
import { fmtUsd, laneFigure, type Unit } from "@/components/ds/unit";
import { useSleeves } from "@/lib/framework/useSleeves";
import { strategyLabel } from "@/lib/framework/position";
import { groupByPosition, positionKeyFor, type Group } from "./grouping";
import { windowDelta, windowNet, type WindowNet } from "@/lib/framework/windowBase";
import { useWindowBase } from "@/lib/framework/useWindowBase";
import { cellHeadline, dayMove, dayMoveByStrategy, isProtected, openedToday, type DayMove } from "./books";

const PORTFOLIO_COLS: DataColumn[] = [
  { label: "Symbol" },
  { label: "Net", align: "right" },
  { label: "Avg", align: "right", className: "hidden sm:table-cell" },
  { label: "Last", align: "right" },
  { label: "P&L", align: "right" },
];

const UNCLAIMED_COLS: DataColumn[] = [
  { label: "Symbol" },
  { label: "Qty", align: "right" },
  { label: "Avg", align: "right", className: "hidden sm:table-cell" },
  { label: "Last", align: "right" },
  { label: "P&L", align: "right" },
];

import { focusFromInstrumentId } from "@/lib/framework/focus";
import { useTodayRanges } from "@/lib/framework/instrument";
import { attribute, deployedValue, floorLabel, isPhantom, money, periodRealizedByStrategy, rowPeriodRealized, visibleBooks, type Book } from "./books";
import type { AccountDTO, TradeDTO } from "@/lib/api/types";
import type { TileProps } from "@/lib/framework/types";

/** The armed row's noun and its explanation (#402), derived from the cycle's own resting orders. */
function _armedOf(group: Group) {
  const orders = group.cycles.flatMap((c) => c.working_orders ?? []);
  return armedReading(orders);
}

function armedNoun(group: Group): string {
  const reading = _armedOf(group);
  return reading ? armedLabel(reading) : "armed";
}

function armedTitle(group: Group): string {
  const reading = _armedOf(group);
  if (reading?.kind === "entry") {
    return (
      `A resting ${reading.side} order for ${reading.qty} that has not triggered. The position is flat, ` +
      "but the order is live at the venue and the capital is committed."
    );
  }
  return (
    "A manager is armed on this symbol. Which mechanism it is — stop-and-reenter, PEAK, pyramid — is not " +
    "on the trades plane; the cycle carries no manager kind. See the Managers view."
  );
}
import type { ManagedPortfolioConfig } from "./definition";
import { TrendStrip } from "@/components/ds/TrendStrip";
import { armedLabel, armedReading } from "./armedReading";
import { failureLabel, managersFor, mechanismLabel } from "./managerReading";
import type { Manager } from "@/lib/api/client";
import { buildTrendWindows } from "@/components/ds/trend";
import { useBars, useInstrument } from "@/lib/framework/instrument";
import { marketSession, type MarketSession } from "@/lib/market";
import { priceState } from "@/lib/priceState";
import { movable, phantomLabel, phantomState } from "./phantom";
import { signedQty } from "@/lib/framework/signedQty";

const STATE_TEXT: Record<string, string> = {
  HELD: "text-status-bull",
  ARMED: "text-status-watch",
  WATCH: "text-status-info",
  CLOSED: "text-t3",
};

// Honest per-origin label for an unclaimed position (not all are "reconciled").
const ORIGIN_LABEL: Record<string, string> = {
  RECONCILIATION: "reconciled",
  VENUE: "manual at broker",
  FOREIGN: "foreign strategy",
  UNKNOWN: "external",
};

/** One side of the split. Colour is never alone — the label and the held count carry it too. */
function BookCell({
  label,
  book,
  period,
  day,
  periodRealized,
  unrealizedDelta,
  net,
  periodLabel,
  unit,
  sleeve,
}: {
  label: string;
  book: Book;
  /** The GLOBAL selector. The cell used to ignore it entirely (#345 item 3). */
  period: string;
  /** Today's move on this strategy's held positions — only meaningful at 1D. */
  day?: DayMove;
  /** Swept realized for the SELECTED window, or null when the sweep has no answer yet (#345 item 3). */
  periodRealized: number | null;
  /**
   * The window's change in this lane's mark (#699/#734), computed by the PARENT because it needs the
   * fetched base map. CARRIED beside the headline, never summed into it — see `cellHeadline`.
   * `null` is unknown: no base captured, the read failed, or an unpriced leg at the base date.
   */
  unrealizedDelta?: number | null;
  /** The lane's window NET OF FLOWS (#699 a) — see BookTile's cell; ONE rule, two tiles. */
  net?: WindowNet | null;
  /** Human name of the window, for the "not swept" explanation. */
  periodLabel: string;
  /** The unit this cell renders relative changes in (#392) — the global toggle's state. The cell used
   *  to render dollars unconditionally while Home obeyed the toggle, so one value read as two. */
  unit: Unit;
  /** THIS LANE'S sleeve, the denominator for its percentages. `null`/absent renders `—` rather than
   *  falling back to the account, which would answer a different question under the same label. */
  sleeve?: number | null;
}) {
  // THE CELL ANSWERS THE SELECTED WINDOW, OR SAYS WHICH WINDOW IT IS ANSWERING.
  //
  // `rows.map` never read `period`, so this rendered lifetime unrealized on open cycles under every tab
  // — Operator, on a 1D tab: "I have selected 1d. so I want to see 1 day."
  //
  // 1D is answerable TODAY: `dayMoveByStrategy` measures qty x (mark - prior close) on what is held,
  // the same basis rule Home uses, so the two screens cannot disagree about the same position.
  //
  // THE LONGER WINDOWS ARE NOT, and this does not pretend otherwise. A window Δunrealized needs a mark
  // per symbol at the window's START, and a position opened INSIDE the window has none — #345 leaves
  // that as an open design fork. Until it is settled, those tabs render the standing figure and SAY SO
  // rather than dressing it as a period number. A wrong label is what produced this report.
  // THE HEADLINE ANSWERS THE SELECTOR (#699), and what it means is decided in ONE place for both
  // tiles — see `cellHeadline`. It used to lead with `book.total`, a standing LEVEL, under a window
  // selector; the windowed figure was in the smallest type on the cell.
  const head = cellHeadline(period, day, periodRealized, unrealizedDelta, net);
  const value = head.value;
  // ONE RULE, TWO TILES — for the UNIT too, now (#392). `laneFigure` is the same helper BookTile's
  // cell calls, against the same lane sleeve, so the two tabs cannot render one number two ways.
  const cellUnit = (v: number | null): string => laneFigure(v, unit, sleeve);
  const cls = value === null ? "text-t3" : value > 0 ? "text-status-bull" : value < 0 ? "text-status-bear" : "text-t2";
  const showDay = head.kind === "day";
  // The standing level is not lost, it is DEMOTED — and it keeps its name, because it is the figure
  // that answers "what is this lane's book worth right now", which the window figure does not.
  const windowLabel = showDay ? "day" : head.kind === "net" ? `net · ${periodLabel}` : `real · ${periodLabel}`;
  return (
    <div className="bg-surf px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-t3">{label}</div>
      <div
        className={`font-mono text-sm ${cls}`}
        title={
          head.kind === "day"
            ? "Today's move on what this lane holds: qty x (mark - prior close). The one window with a true per-lane delta."
            : head.kind === "net"
              ? `ΔMV − flows over ${periodLabel}: the change in this lane's market value net of what it invested (buys − sells) — the same identity as DELTA NET above, with no cost basis in it. Realized (FIFO) and the change in mark ride in the small print.${head.partial ? ` PARTIAL — ${head.partial}.` : ""}`
            : head.kind === "unswept"
              ? `The ${periodLabel} window has not been swept yet — this is unknown, not zero.`
              : `REALIZED over ${periodLabel} — money from positions CLOSED in the window. Not the same composition as DELTA NET above, which also carries the change in unrealized; the window's net of flows is unknown here: ${net?.reason ?? "no net terms were fetched"} (#699).`
        }
      >
        {cellUnit(head.value)}
        {head.kind === "net" && head.partial && (
          <span className="text-t3" title={`PARTIAL — ${head.partial}`}>
            ~
          </span>
        )}
        {book.unknown > 0 && (
          <span
            className="text-t3"
            title={`${book.unknown} held position${book.unknown > 1 ? "s" : ""} without a mark — unrealized excluded`}
          >
            +
          </span>
        )}
      </div>
      {/* THE DAY MOVE IS MISSING, AND THAT IS NOT THE SAME AS FLAT (#298). The `today_ranges` plane
          empties or tombstones and `covered` drops to 0 for every strategy at once, so the cell
          shows realized(1D) instead — a different quantity under the same selector. Rendered from
          `cellHeadline`'s OWN reading, not from a second `readDayMove` call, so the two tiles cannot
          disagree about whether today is unavailable. */}
      {/* THE DAY LINE, from the SAME reading the headline used (#699 review).
          Two things this gets right that the first cut did not:
          - at 1D-covered the headline IS the day move, so repeating it here was the duplication
            just removed from the sub-line, reintroduced one line up. Shown only when it ADDS
            something: the "+" marking a figure understated by positions with no prior close.
          - at every OTHER window it still renders, because "today moved -$393.54" is a real fact a
            reader wants under a 1M headline. My first cut gated the whole block on `kind === "day"`
            and silently deleted it from 1W/1M/3M/All — a fact removed from the screen with nothing
            saying so. */}
      {head.day.kind === "value" && !(head.kind === "day" && head.day.missing === 0) && (
        <div
          className={`font-mono text-[10px] ${head.day.value > 0 ? "text-status-bull" : head.day.value < 0 ? "text-status-bear" : "text-t2"}`}
        >
          day {fmtUsd(head.day.value)}
          {head.day.missing > 0 && (
            <span className="text-t3" title={`${head.day.missing} without a prior close — understated`}>
              +
            </span>
          )}
        </div>
      )}
      {/* THE DAY MOVE IS MISSING, AND THAT IS NOT THE SAME AS FLAT (#298). The `today_ranges` plane
          empties or tombstones and `covered` drops to 0 for every strategy at once. Rendered from the
          shared reading so both tiles say it — ManagedPortfolioTile said nothing at all before. */}
      {head.day.kind === "unavailable" && (
        <div
          className="font-mono text-[10px] text-status-watch"
          title={`${head.day.missing} held position${head.day.missing > 1 ? "s" : ""} with no prior close on the today_ranges plane, so today's move cannot be computed. This is a missing feed, not a flat day.`}
        >
          day — no prior close
        </div>
      )}
      {/* THE SUB-LINE CARRIES WHAT THE HEADLINE IS NOT (#699 review).
            Two things it got wrong first time round:
            - in the DAY case the trailing realized lost its window name, so an unlabelled realized
              sat under a selector — the complaint class itself, in small print;
            - in the WINDOW case it REPEATED the headline's own figure, and `real · 1M · -$28.20
              standing` invited reading -$28.20 as the window's realized.
            So the headline's descriptor is separated from the standing level by an em dash, and the
            realized figure appears once: in the headline when the headline IS it, in the sub-line
            (named) when the headline is the day move. */}
      <div className="font-mono text-[10px] text-t3">
        {book.held} held · {windowLabel}
        {/* THE UNPRICED COUNT IS GONE FROM HERE, DELIBERATELY (#699 review).
            It was a DAY-plane fact living in the WINDOW descriptor. Repointing it at the shared
            reading also silently dropped its `showDay` gate, so it began rendering under 1W/1M/3M/All
            as "real · 1M · 1 unpriced", which reads as "the 1M realized is missing prices" — it is
            not. A right number made misreadable is the complaint this whole ticket is about.

            It was also a duplicate: the day line above already carries the same fact as its "+",
            with the tooltip, at every window and on BOTH tiles — this fragment existed only here, so
            the two tiles disagreed in exactly the state the shared helper was built to unify. One
            derivation, one render. */}
        {/* UNREALIZED — the same word means the same quantity here, in BookTile and in the header
            (#808 item 1). Two tiles rendering "standing" from two fields is the drift this repo pays for. */}
        {` — standing ${fmtUsd(book.unrealized)}`}
        {!(showDay || head.kind === "net") ? null : (
          <>
            {" · real "}
        {/* THE SWEPT FIGURE FOR THE SELECTED WINDOW, not the live-cycle sum (#345 item 3).
            `book.realized` adds up `realized_pnl` over the cycles still in the projection, which is a
            SESSION number: a closed cycle is emitted once and dropped, and reconciliation restores only
            OPEN positions, so after any restart every cell here read `real $0.00` while the book had
            realized thousands. It also ignored the period tabs entirely.

            THREE STATES, NEVER TWO. `null` is "this window has not been swept" and renders as an em
            dash; a swept window in which this strategy realized nothing is a real 0 and prints as
            $0.00. Rendering the unknown as zero is the claim this repo keeps paying for — it is what
            made MANUAL-001's +1,509.72 look like a lane that had made nothing. */}
            {periodRealized == null ? (
              <span title={`The ${periodLabel} window has not been swept yet — this is unknown, not zero.`}>—</span>
            ) : (
              cellUnit(periodRealized)
            )}
            {` · ${periodLabel}`}
          </>
        )}
      </div>
    </div>
  );
}

function PortfolioRow({
  group,
  onOpen,
  nowMs,
  session,
  managers,
}: {
  group: Group;
  onOpen: () => void;
  nowMs: number;
  /** Computed once by the tile — one clock, every row agrees (#356). */
  session: MarketSession;
  /** Every manager the engine holds; scoped to this row's CURRENT cycle inside (#402/#400). */
  managers: Manager[];
}) {
  // Scoped to the CURRENT cycle — see `managersFor`. A row can carry several cycles; the manager
  // question is asked per cycle, so this asks it for each and flattens.
  const reading = group.cycles.map((c) => managersFor(managers, c.instrument_id, c.strategy_id, c.cycle_id));
  const mechanisms = [...new Set(reading.flatMap((r) => r.live.map((m) => mechanismLabel(m.kind))))];
  const failures = reading.flatMap((r) => r.failed);

  // TRENDLINES ON THE SCREEN YOU ACTUALLY HOLD POSITIONS ON (#251). The operator asked for this on 2026-08-12 and
  // it was blocked by the issue's own condition: "do not add it to a second tile until [the width] is
  // resolved, or the same problem doubles". The strip was ~250px and would not shrink. It is now fluid
  // (#294), so the blocker is gone and this is wiring the same shared primitive to bars we already stream.
  //
  // The SAME six windows as the watchlist, deliberately. #251 wondered whether a held name wants a
  // shorter set (1D/1W/1M) — but one primitive showing different horizons on two screens is a trap: the
  // strips look identical and mean different things, and nothing on either screen says which. If the
  // shorter set is wanted it should be a config on the primitive, chosen once, not diverged by tile.
  const m1Bars = useBars(group.instrumentId, "1m");
  const h1Bars = useBars(group.instrumentId, "1h");
  const d1Bars = useBars(group.instrumentId, "1d");
  const w1Bars = useBars(group.instrumentId, "1w");
  const toTrendBars = (bs: { ts_event: number; close: number }[]) =>
    bs.map((b) => ({ ts: b.ts_event, close: b.close }));
  const trendWindows = buildTrendWindows({
    m1: toTrendBars(m1Bars),
    h1: toTrendBars(h1Bars),
    d1: toTrendBars(d1Bars),
    w1: toTrendBars(w1Bars),
  });
  const netCls = group.netQty > 0 ? "text-status-bull" : group.netQty < 0 ? "text-status-bear" : "text-t2";
  // Today's move on the HOLDING — qty × (mark − prior close), the same prior close the watchlist reads,
  // so one name never shows two different day changes on two screens.
  // Reuses `dayMove`, the same basis rule the Home book tile uses (#270): a cycle opened TODAY is
  // measured from ITS ENTRY, everything else from the prior close. Computing it here as
  // `netQty × (last − priorClose)` made this tile disagree with Home on the same position — and on a
  // name bought part-way through a move it credited the whole day's move rather than the part actually
  // captured. Splitting rows by strategy narrowed that; it did not fix it, because one strategy can hold
  // a carried cycle and a same-day one at once. (codex review, Medium.)
  const dayResult = dayMove(
    group.cycles,
    new Map(group.priorClose != null ? [[group.instrumentId, group.priorClose]] : []),
    nowMs,
  );
  const day = dayResult.covered > 0 ? dayResult.value : null;
  // The percent must share the DOLLARS' basis or the pair contradicts itself (codex review, Medium).
  // For a position opened today the dollars are entry-based, while a prior-close percent describes the
  // whole symbol's day - an NBIS-style row would read "+$46" beside "+29%". Rather than invent a blended
  // figure, the percent is withheld when any cycle in the row was opened today; the dollars are the
  // honest number and the symbol's own day change is a click away on the chart.
  const hasSameDayCycle = group.cycles.some((c) =>
    openedToday((c as { opened_ts?: number | null }).opened_ts, nowMs),
  );
  // THE PERCENT POINTS THE SAME WAY AS THE DOLLARS BESIDE IT (#855). `dayMove` signs the dollars by
  // side — deliberately, `books.ts:423` — and this read the SYMBOL's move with no side at all, so a
  // short whose mark rose rendered "day -$100.00 (+10.00%)": the two halves of one cell contradicting
  // each other about whether today hurt. The sign now comes from `day` itself rather than from a
  // second reading of the side, so the pair cannot drift; the percent is withheld whenever the
  // dollars are, because a percentage with no amount beside it has no basis the operator can check.
  // THE SIGN COMES FROM THE ROW'S DIRECTION, NOT FROM WHETHER THE DOLLARS HAPPENED TO RESOLVE.
  //
  // Taking it from `Math.sign(day)` also tied the percent's EXISTENCE to `day`, and `dayMove` returns
  // null for a cycle with no `last_px` even when the shared prior-close plane has both numbers this
  // needs. A long row like that used to show a percent and showed nothing at all afterwards — a
  // regression on the side the ticket was not about.
  //
  // `netQty` is the honest source: under NETTING a group is one instrument and one strategy
  // (`grouping.ts:47-57`), so it is single-sided, and `hasSameDayCycle` already forces every
  // remaining leg's basis to the prior close — which makes this sign identical to `Math.sign(day)`
  // wherever `day` exists, and available where it does not.
  const dayPct =
    !hasSameDayCycle && group.last != null && group.priorClose
      ? Math.abs(((group.last - group.priorClose) / group.priorClose) * 100) * (Math.sign(group.netQty) || 1)
      : null;
  const dayCls = day == null ? "text-t3" : day > 0 ? "text-status-bull" : day < 0 ? "text-status-bear" : "text-t3";
  // P&L column = TOTAL (realized + unrealized); the sub-line breaks it into unreal/real.
  // #356's portfolio half. VCTR at 08:49 ET had not traded that day: its "current price" was yesterday's
  // close carried forward and its intraday P&L read exactly +0.00 — which this tile rendered as FLAT. The
  // truth was UNPRICED, and those are different facts. `priceState` decides which before it is formatted.
  // TODAY'S RANGE HAS TO COME FROM THE SAME PLANE THE WATCHLIST READS. Passing only `prevClose` made
  // `hasTradedToday` unconditionally false, so every held row would have read "Prior close" all through
  // the regular session — mislabelling a genuinely traded price, which is the mirror image of the bug
  // this is fixing. Caught by looking at the rendered row, not by a test.
  const { todayRange } = useInstrument(group.instrumentId);
  const priced = priceState({
    price: group.last,
    session,
    todayRange: { ...(todayRange ?? {}), prevClose: group.priorClose ?? todayRange?.prevClose ?? null },
    quote: null,
  });
  const marked = group.unrealized != null;
  const hasPnl = marked || group.realized !== 0;
  const p = pnlOf(group.realized, group.unrealized);
  // THE BROKER CONTRADICTS THE ENGINE ABOUT THIS POSITION'S COST BASIS (#370). WHD reported +$263.84
  // unrealized while Alpaca said +$9.52 — $254.32 of gain that did not exist — and the only trace was a
  // WARN in the engine log, invisible here. The engine now marks against the broker; this says so, the
  // way SECURED says how many holdings are unprotected. Three-state: `false` is "checked, they agree"
  // and `null` is "not checked", and only `true` earns a badge.
  const contested = group.cycles.find(
    (c) => (c as { basis_contested?: boolean | null }).basis_contested === true,
  ) as { venue_avg_px?: number | null } | undefined;
  return (
    <DataRow
      columns={PORTFOLIO_COLS}
      onClick={onOpen}
      cells={[
        <span key="s" className="flex min-w-0 flex-col leading-tight">
          <span className="text-[13px] font-bold text-t1">{group.symbol}</span>
          {/* Named on the row itself: two rows can now share a symbol, and which SLEEVE holds it is the
              thing that distinguishes them (#273). */}
          <span className="font-mono text-[9px] uppercase tracking-wide text-t3">
            {strategyLabel(group.strategyId)}
          </span>
        </span>,
        <span key="n" className={netCls}>{group.netQty || "flat"}</span>,
        <span key="a" className="text-t2">{group.avg != null ? `$${group.avg.toFixed(2)}` : "—"}</span>,
        <span key="l" className="flex flex-col items-end leading-tight">
          <span className="text-t1">{group.last != null ? `$${group.last.toFixed(2)}` : "—"}</span>
          {/* Provenance in TEXT (#355 R2). Silent for an ordinary regular-hours trade — a label on every
              row is noise; a label when the number is a carried-forward close is the whole point. */}
          {priced.kind !== "traded" && (
            <span className="font-mono text-[8px] uppercase tracking-wide text-t3">{priced.label}</span>
          )}
        </span>,
        <span key="p" className={hasPnl ? p.cls : "text-t3"}>{hasPnl ? fmtUsd(p.total) : "—"}</span>,
      ]}
      sub={
        // Free-form line: held/armed state, the unreal/real P&L split, per-strategy attribution.
        <span className="flex flex-wrap gap-x-2 gap-y-0.5">
          <span className="text-status-bull">{group.held} held</span>
          {group.armed > 0 && (
            // A COUNT WITH NO NOUN was all this said (#402). "1 armed" cannot distinguish a resting
            // entry that has not triggered from a stop-and-reenter waiting on a reclaim from a PEAK
            // manager still attached to a flattened position — and two of those mean money is committed
            // at the venue. The resting entry comes from the cycle's own working orders; the MECHANISM
            // now comes from the managers plane, because `manager_id` is null on the cycle.
            <span className="text-status-watch" title={armedTitle(group)}>
              · {group.armed} {armedNoun(group)}
              {mechanisms.length > 0 && ` · ${mechanisms.join(", ")}`}
            </span>
          )}
          {/* A FAILED MANAGER HAD NO SURFACE AT ALL (#400/#401). Both live failures on 2026-08-21 —
              A.XNYS's rearm dying on an off-tick price, and MNDY's before it — were invisible here and
              were only found by querying Postgres.

              Scoped to THIS cycle, which is what keeps it an alarm rather than noise: a `peak_watch`
              that failed against a cycle closed nine days ago is history, and rendering it beside a
              live one is exactly the misreading that produced a false "PEAK is dead" report. The age
              is shown because "failed 3m ago" and "failed 9d ago" are different facts. */}
          {failures.map((f) => (
            <span
              key={f.manager_id}
              className="whitespace-nowrap text-status-bear"
              title={`${mechanismLabel(f.kind)} on this position's current cycle failed and will not act. ${f.error ?? "No reason recorded."}`}
            >
              · {failureLabel(f, nowMs)}
            </span>
          ))}
          <span className="text-t3">· unreal {marked ? fmtUsd(p.unrealized) : "—"} · real {fmtUsd(group.realized)}</span>
          {contested && (
            <span
              className="whitespace-nowrap text-status-watch"
              title="The broker's cost basis for this position disagrees with the engine's. P&L here is marked against the BROKER, which is the only hard reconciliation anchor."
            >
              · basis contested
              {contested.venue_avg_px != null ? ` — broker $${contested.venue_avg_px.toFixed(2)}` : ""}
            </span>
          )}
          {day != null && (
            <span className={dayCls} title="Today's move on this holding. A position opened today is measured from its entry; one carried over, from the prior close.">
              · day {fmtUsd(day)}
              {dayPct != null && ` (${dayPct >= 0 ? "+" : ""}${dayPct.toFixed(2)}%)`}
            </span>
          )}
          {group.cycles.map((c) => {
            // THE MODE, ON THE ROW (#872). A lane on `entry_floor` shows "$0.00 secured" forever —
            // the tally counts only stops ABOVE entry and a floor sits below one by construction — so
            // without the label a correctly protected position reads as an unprotected one. The
            // trigger comes with it: "floor" alone does not say where it sits.
            const floor = floorLabel(c);
            return (
              <span key={c.cycle_id} className="whitespace-nowrap">
                <span className="text-t2">{c.strategy_id}</span>{" "}
                <span className={STATE_TEXT[c.state] ?? "text-t3"}>{c.state}</span>
                {floor && (
                  <span
                    className="text-status-info"
                    title="An ENTRY FLOOR: a fixed stop at this strategy's own entry minus 1.5x ATR, set once when it was placed and never raised. It limits loss rather than locking in gain, so SECURED counts nothing for it — that is intended, not a gap."
                  >
                    {" "}· {floor}
                  </span>
                )}
              </span>
            );
          })}
          <span className="w-full">
            <TrendStrip windows={trendWindows} />
          </span>
        </span>
      }
    />
  );
}

interface ExternalItem {
  instrument_id: string;
  source: string; // "POSITION" | "ORDER"
  side: string; // "LONG" | "SHORT" | "BUY" | "SELL"
  quantity: number;
  origin: string; // "RECONCILIATION" | "VENUE" | "FOREIGN" | "UNKNOWN"
  strategy_id: string; // the non-owned id — only EXTERNAL can be moved in v0
  realized_pnl?: string | null; // Money string
  avg_px?: number | null;
  last_px?: number | null;
  market_value?: number | null;
  unrealized_pl?: number | null;
  unrealized_plpc?: number | null;
  // THE BROKER'S QUANTITY (#807 item 3): 0 = answered, holds none (phantom); null/undefined = not
  // answered. Declared here because a narrow local type that omits a published key is the TypeScript
  // form of the Pydantic-drop defect — the tile would read `undefined` forever while everything compiles.
  venue_qty?: number | null;
}

/** total (realized + unrealized) P&L, and its tint class. */
function pnlOf(realized: number, unrealized: number | null): { total: number; unrealized: number; cls: string } {
  const u = unrealized ?? 0;
  const total = realized + u;
  const cls = total > 0 ? "text-status-bull" : total < 0 ? "text-status-bear" : "text-t2";
  return { total, unrealized: u, cls };
}

/** `all` · `unprotected` · or a strategy id. A union rather than two independent controls: they are
 *  mutually exclusive views of one list, and two chip rows would imply they compose when they do not. */
type PortfolioFilter = "all" | "unprotected" | string;

export function ManagedPortfolioTile({ data, status: srcStatus }: TileProps<ManagedPortfolioConfig>) {
  const openDetailForSymbol = useCockpitStore((s) => s.openDetailForSymbol);
  const openDetail = useCockpitStore((s) => s.openDetail);
  // EVERY KEY THE ENGINE PUBLISHES MUST BE DECLARED HERE — the same rule BookTile's frame carries. A
  // narrow local type is the TypeScript form of the Pydantic-drops-a-field defect (#233 / #322 / #336):
  // the engine publishes it, the declaration omits it, and the tile reads `undefined` forever while
  // everything compiles. `by_strategy` is what #523 needs.
  const tradesFrame = data.trades as
    | {
        trades?: TradeDTO[];
        realized_periods?: Record<
          string,
          // `unclaimed` is #596's money and `rowPeriodRealized` reads it. Omitting it here while the
          // comment above demands every published key is the same drift the comment exists to stop.
          { by_strategy?: Record<string, number> | null; unclaimed?: number | null } | null
        > | null;
      }
    | undefined;
  const trades = (tradesFrame?.trades ?? []).filter((t) => t.is_engaged);
  // The account WS source delivers the snapshot envelope { account: AccountDTO | null }, not the DTO directly.
  const account = (data.account as { account?: AccountDTO | null } | undefined)?.account ?? undefined;
  // The managers plane (#402). Bound by name in `definition.ts`; the tile never fetches.
  const managers = (data.managers as { managers?: Manager[] } | undefined)?.managers ?? [];
  // Broker positions the cockpit didn't claim to a strategy (#79) — e.g. reconciled from the broker after a
  // restart lost their originating orders. They're real holdings; surface them so an unclaimed position is
  // never invisible (it only "vanished" from the managed table because it has no strategy attribution).
  const externalAll = ((data.external_activity as { external?: ExternalItem[] } | undefined)?.external ?? []).filter(
    (x) => x.source === "POSITION",
  );
  // THE TABLE LISTS EVERY ROW (a phantom must stay visible, labelled); THE SUMS SKIP PHANTOMS (#808
  // item 4): stock the broker holds none of is neither held, nor deployed, nor P&L.
  const external = externalAll.filter((x) => !isPhantom(x));
  const groups = groupByPosition(trades);
  // FILTERS (#251). Operator, 2026-08-12: "add filters". The config already declared a `strategy` field —
  // "unused until multi-strategy lands" — and multi-strategy landed: four strategies run today, and the
  // flat list carries the tag on every row precisely because it cannot group by it.
  //
  // `unprotected` is the axis the issue calls the one that matters, and it uses `isProtected` — the
  // SAME predicate the SECURED tally uses — rather than a second copy. A filter that says "unprotected"
  // while SECURED says covered is a safety claim that is wrong on one of the two screens showing it.
  const [filter, setFilter] = useState<PortfolioFilter>("all");
  const strategies = Array.from(new Set(groups.map((g) => g.strategyId))).sort();
  const shown = groups.filter((g) => {
    if (filter === "all") return true;
    if (filter === "unprotected") return g.cycles.some((c) => c.is_capital_deployed && !isProtected(c));
    return g.strategyId === filter;
  });
  // ONE clock for the whole table. Captured per row, a render straddling ET midnight could apply the
  // same-day rule to some rows and not others, flipping the day basis and the percent inconsistently
  // against an unchanged `today_ranges` snapshot. (codex review, Low — the same fix BookTile needed.)
  const nowMs = Date.now();
  // One session for every row (#356). `nowMs` here is recomputed on each render of the tile, which the
  // live planes drive often enough — but the SESSION must not be derived per row: twenty answers to one
  // question is how one row reads "Pre-market" after 09:30 while its neighbour reads "Traded".
  const session = marketSession(nowMs);

  const engagedNames = groups.length;
  // Held = managed HELD names + unclaimed broker positions. Counting unclaimed here is the honest number —
  // saying "0 held" while the broker holds FIG/CVS is a lie.
  const deployedNames = groups.filter((g) => g.held > 0).length + external.length;
  const equity = account?.equity ?? null;
  // % deployed = market value of ALL held (managed + unclaimed) / equity — else it reads 0% while holding.
  // A held name with no mark contributes NOTHING to these sums, which silently understates both.
  // `?? 0` reads as "worth zero" when it means "not known yet" — a position waiting on its first
  // tick, or a symbol whose quote plane has not arrived. The totals stay computed, because a
  // partial number beats a blank header, but they are marked incomplete so the figure is not read
  // as the whole book.
  // ONE derivation of deployed capital, and it is `deployedValue`'s (#855).
  //
  // This was a THIRD reading of the same number, in the same file that already imports the function
  // that answers it. Its managed half took |netQty| × last — the committed-capital convention — while
  // its unclaimed half took the row's RAW market value, so an unclaimed short SUBTRACTED here while
  // the identical row ADDED in `deployedValue`. Measured on a rendered tile: one short of $1,100
  // against $22,000 of equity made the header read "-5%", an account minus five percent deployed.
  //
  // `deployedValue` also prefers the server-marked `market_value` over recomputing |qty| × last_px,
  // which is the rule `books.ts` already states and which this reading quietly did not follow.
  const deployed = deployedValue(trades, external);
  const unmarked = deployed.unmarked;
  const heldValue = deployed.value;
  const pctDeployed = equity && equity > 0 ? (heldValue / equity) * 100 : null;
  // Total P&L (realized + unrealized) across the whole book — managed cycles + unclaimed.
  const pnlUnknown =
    groups.filter((g) => g.held > 0 && g.unrealized == null).length +
    external.filter((x) => x.unrealized_pl == null).length;
  const totalPnl =
    groups.reduce((s, g) => s + g.realized + (g.unrealized ?? 0), 0) +
    external.reduce((s, x) => s + money(x.realized_pnl) + (x.unrealized_pl ?? 0), 0);
  const totalPnlCls = totalPnl > 0 ? "text-status-bull" : totalPnl < 0 ? "text-status-bear" : "text-t3";
  // Prior closes for today's move — same shared plane the watchlist reads, so a name cannot show two
  // different day changes on two screens.
  const priorCloses = useTodayRanges();
  // THE SELECTOR THE CELLS USED TO IGNORE (#345 item 3). Same store the Book tile and the period tabs
  // read, so one selection drives every surface rather than each tile answering its own window.
  const period = useCockpitStore((s) => s.period);
  // THE GLOBAL TOGGLE (#392). One store, every screen — a per-tile unit state would make the two tabs
  // disagree in the other direction.
  const unit = useCockpitStore((s) => s.unit);
  // THE DENOMINATORS, per lane. A lane card is about the LANE, so its percentage is of its own sleeve
  // and never of the account (#586): the same $1,144.60 is +5.7% of one and +1.1% of the other, and
  // they answer different questions.
  const sleeves = useSleeves();
  // Per-StrategyId attribution — see `attribute`. Shown only when more than one book has something to
  // say; with a single book the breakdown restates the header total, which is noise.
  //
  // DECLARED AFTER `period`, because the row list depends on the selected window (#523). The rows come
  // from live TRADE CYCLES, and a CLOSED cycle is emitted once and dropped from the projection — so a
  // strategy holding nothing has no cycle, no book, and no row, and its realized P&L renders nowhere.
  // Passing the window's swept `by_strategy` map renders the UNION of live-cycle and swept strategies
  // instead of their intersection. Measured on paper 2026-08-29: MANUAL-001 was flat and +1509.72 over
  // 1M — the second-largest realized contributor in the book — and was not on this tile at all, while
  // BookTile (fixed for #523) showed it. Two panels, same question, different answers.
  const bookRows = visibleBooks(
    attribute(trades, external),
    periodRealizedByStrategy(tradesFrame, period),
    // The swept unclaimed figure, so EXTERNAL money still lands somewhere when no live unclaimed
    // position exists to raise the row on its own (#596, one row over).
    rowPeriodRealized(tradesFrame, period, "Unclaimed"),
  );
  // Today's move per strategy, from the SAME function Home uses — two derivations of "today's move"
  // would let the two screens disagree about the same position, which #270 already fixed once.
  // The per-lane window base (#699/#734). Its own query — it reads a database and its answer changes
  // once a day, so it must not ride the continuously-pushed trade frame.
  const windowBase = useWindowBase();
  const dayByStrategy = dayMoveByStrategy(trades, priorCloses, Date.now());
  for (const g of groups) g.priorClose = priorCloses.get(g.instrumentId) ?? null;

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-t2">Portfolio</h2>
        <span className="font-mono text-xs text-t3">
          {engagedNames + external.length} engaged · {deployedNames} held
          {pctDeployed != null && ` · ${pctDeployed.toFixed(0)}%${unmarked ? "+" : ""}`}
          {totalPnl !== 0 && (
            <span className={totalPnlCls}>
              {" · "}
              {fmtUsd(totalPnl)}
              {pnlUnknown > 0 && (
                <span className="text-t3" title={`${pnlUnknown} held position${pnlUnknown > 1 ? "s" : ""} without a mark — excluded from this total`}>
                  +
                </span>
              )}
            </span>
          )}
        </span>
      </div>

      {/* One row of chips, only when there is something to choose between. A filter control on a
          single-strategy book is chrome that never earns its space. `Unprotected` is listed last and
          reads as a state rather than a book, because it cuts across the strategies rather than
          partitioning them. */}
      {(strategies.length > 1 || groups.length > 0) && (
        <div className="mb-2 flex flex-wrap gap-1">
          {(["all", ...strategies, "unprotected"] as PortfolioFilter[]).map((key) => {
            const on = filter === key;
            const label =
              key === "all" ? "All" : key === "unprotected" ? "Unprotected" : strategyLabel(key);
            const n =
              key === "all"
                ? groups.length
                : key === "unprotected"
                  ? groups.filter((g) => g.cycles.some((c) => c.is_capital_deployed && !isProtected(c))).length
                  : groups.filter((g) => g.strategyId === key).length;
            return (
              <button
                key={key}
                type="button"
                onClick={() => setFilter(on ? "all" : key)}
                className={`shrink-0 rounded px-2 py-0.5 font-mono text-[10px] transition-colors ${
                  on ? "bg-ds-surf2 text-t1" : "text-t3 hover:text-t2"
                }`}
              >
                {/* The COUNT ships with the label. A filter that hides everything looks broken unless
                    the chip already said it would match nothing. */}
                {label} {n}
              </button>
            );
          })}
        </div>
      )}

      {bookRows.length > 0 && (
        <div className="mb-3 grid grid-cols-2 gap-px overflow-hidden rounded-md border border-line bg-line sm:grid-cols-3">
          {bookRows.map((r) => (
            <BookCell key={r.label} label={r.label} book={r.book} period={period} // `visibleBooks` uses the strategyId AS the label; the "Unclaimed" row simply misses, which
              // is right — it has no strategy whose day move could be measured.
              day={dayByStrategy.get(r.label)}
              // THE SAME derivation BookTile uses — one helper, not a second copy (#596 is what a
              // second copy costs). `r.label` IS the strategy id, except "Unclaimed", which the
              // helper routes to EXTERNAL + residual.
              periodRealized={rowPeriodRealized(tradesFrame, period, r.label)}
              unrealizedDelta={windowDelta(r.book.unrealized, windowBase?.by_period?.[period], r.label)}
              net={windowNet(r.book.marketValue, windowBase?.net?.[period], r.label)}
              periodLabel={PERIODS.find(([k]) => k === period)?.[1] ?? period}
              unit={unit}
              sleeve={sleeves[r.label]} />
          ))}
        </div>
      )}

      {/* EMPTINESS READS THE UNFILTERED LIST (codex, #808): a book whose only unclaimed rows are phantoms
          must still SHOW them — labelled — not render "No managed names" over an invisible table. */}
      <TileState status={srcStatus.trades} isEmpty={groups.length === 0 && externalAll.length === 0} emptyLabel="No managed names">
        {groups.length > 0 && (
          <DataTable columns={PORTFOLIO_COLS}>
            {shown.map((g) => (
              <PortfolioRow
                key={JSON.stringify([g.instrumentId, g.strategyId])}
                session={session}
                group={g}
                nowMs={nowMs}
                managers={managers}
                // A held position opens the POSITION detail, not the symbol/entry surface. Tapping
                // something you already own to be shown a BUY ticket is the canonical mistake here — for a
                // holding the primary actions are exit/trim/adjust-stop.
                //
                // A row is now ONE position, so there is nothing left to guess: the fallback to the
                // symbol surface remains only for a row with nothing held (flat/ARMED). Previously a row
                // held by two strategies also fell back, which meant the multi-strategy positions could
                // not be opened at all (#273).
                onOpen={() => {
                  const key = positionKeyFor(g);
                  if (!key) {
                    openDetailForSymbol(focusFromInstrumentId(g.instrumentId, { tab: "portfolio" }, g.symbol));
                    return;
                  }
                  openDetail({
                    kind: "position",
                    positionKey: key,
                    instrumentId: g.instrumentId,
                    context: { tab: "portfolio", strategy_id: key.split(":")[0] },
                  });
                }}
              />
            ))}
          </DataTable>
        )}

        {/* Unclaimed broker positions (#79): real holdings not attributed to a strategy — on the SAME
            DataTable primitive as the managed table (frozen symbol, main row + sub-line). Tap → symbol detail. */}
        {externalAll.length > 0 && (
          <div className={groups.length > 0 ? "mt-4" : ""}>
            <div className="mb-2 flex items-baseline justify-between">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-t2">Unclaimed</h3>
              <span className="font-mono text-[10px] text-t3">broker positions · no strategy</span>
            </div>
            <DataTable columns={UNCLAIMED_COLS}>
              {externalAll.map((x) => {
                const sym = x.instrument_id.split(".")[0];
                const marked = x.unrealized_pl != null;
                const hasPnl = marked || money(x.realized_pnl) !== 0;
                const p = pnlOf(money(x.realized_pnl), x.unrealized_pl ?? null);
                const pct = x.unrealized_plpc != null ? ` (${x.unrealized_plpc > 0 ? "+" : ""}${(x.unrealized_plpc * 100).toFixed(2)}%)` : "";
                // Honest origin label — not every unclaimed row is "reconciled" (VENUE/FOREIGN differ).
                const originLabel = ORIGIN_LABEL[x.origin] ?? "external";
                const phantom = phantomState(x.venue_qty);
                return (
                  <DataRow
                    // Instrument alone is not unique here either: the external plane can emit two open
                    // positions for one instrument under different strategy ids or sides, and colliding
                    // React keys attach the wrong row state and click target. (codex review, Low.)
                    key={JSON.stringify([x.instrument_id, x.strategy_id ?? "", x.side ?? ""])}
                    columns={UNCLAIMED_COLS}
                    // Unclaimed rows open the UNCLAIMED detail — assigning a position to a strategy is a
                    // decision that wants the numbers and the consequences in front of you, not a row button.
                    // A PHANTOM CANNOT BE MOVED (#807 item 3): the broker holds none of it, so "move to a
                    // strategy" would claim shares that do not exist. `phantomState` is the one derivation
                    // the label below also reads.
                    onClick={
                      !movable(phantom)
                        ? undefined
                        : () =>
                            openDetail({
                              kind: "unclaimed",
                              instrumentId: x.instrument_id,
                              sourceStrategyId: x.strategy_id,
                              side: x.side,
                              context: { tab: "portfolio" },
                            })
                    }
                    cells={[
                      <span key="s" className="text-[13px] font-bold text-t1">{sym}</span>,
                      // The magnitude, like every other quantity this board renders (#855). The decode
                      // already absolutises it; a render site that would print "-10" when handed a row
                      // from a store or a mock is wrong on its own terms. The rule is in
                      // `normaliseQuantity.ts`: the decode is the class-closer, the render sites are
                      // belt and braces, and they must be consistent with each other.
                      <span key="q" className="text-t1">{Math.abs(signedQty(x.side, x.quantity))}</span>,
                      <span key="a" className="text-t2">{x.avg_px != null ? `$${x.avg_px.toFixed(2)}` : "—"}</span>,
                      <span key="l" className="text-t1">{x.last_px != null ? `$${x.last_px.toFixed(2)}` : "—"}</span>,
                      <span key="p" className={hasPnl ? p.cls : "text-t3"}>{hasPnl ? fmtUsd(p.total) : "—"}</span>,
                    ]}
                    sub={
                      <span>
                        {/* Colour and side from ONE derivation, so the badge cannot disagree with the
                            quantity beside it (#855). */}
                        <span className={signedQty(x.side, x.quantity) < 0 ? "text-status-bear" : "text-status-bull"}>{x.side}</span>
                        {x.market_value != null && <> · mkt {fmtUsd(x.market_value)}</>}
                        {pct && <span className={p.cls}>{pct}</span>}
                        <span className={phantom === "phantom" ? "text-status-bear" : phantom === "unconfirmed" ? "text-status-warn" : undefined}>
                          {` · ${phantomLabel(phantom, originLabel)}`}
                        </span>
                      </span>
                    }
                  />
                );
              })}
            </DataTable>
          </div>
        )}
      </TileState>
    </div>
  );
}
