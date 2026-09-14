"use client";

/**
 * BookTile — the P&L hero Option A puts at the top of Home (PR #223, epic #212, #107).
 *
 * Answers "what is the money doing", split by WHO decided it. The account header carries one total
 * across everything, and that single figure is the one most likely to be misread: right now it reads
 * ~$1,890 while a quarter of that is a manual FIG position the strategy never touched. On 2026-08-10
 * the same conflation ran the other way — a manual winner masking an automated book down $1,289.
 *
 * Deliberately NOT the portfolio table. Home answers whether the strategy needs you; the per-name
 * breakdown lives one tab away in the Portfolio tile, which shares this tile's `books.ts` logic so
 * the two can never disagree.
 *
 * Read-only. Nothing here submits, cancels, pauses or arms.
 */
import { noneAboveEntry, securedSub } from "./securedSub";
import { TileState } from "@/components/board/TileState";
import { feedState } from "@/lib/framework/feedState";
import { disputedBadge } from "@/lib/framework/ownership";
import { windowDelta, windowNet, type WindowNet } from "@/lib/framework/windowBase";
import { useWindowBase } from "@/lib/framework/useWindowBase";
import { cellHeadline, attribute, dayMove, dayMoveByStrategy, deployedValue, money, securedValue, periodRealized as periodRealizedOf, periodRealizedPartial, periodHorizonTs, periodBrokerRealized, periodUnclaimedRealized, readDayMove, rowPeriodRealized, periodRealizedByStrategy, visibleBooks, type Book, type DayMove, type SessionRealizedLike } from "@/tiles/managed-portfolio/books";
import { cadenceNote } from "./cadenceNote";
import { marketAwareBadge } from "./marketAwareBadge";
import { useHealth } from "@/lib/framework/health";
import { useTodayRanges } from "@/lib/framework/instrument";
import { useCockpitStore } from "@/lib/framework/store";
import { PERIODS } from "@/components/ds/periods";
import { BOOK_FIGURES, fmtPct, fmtUsd, laneFigure, percentOf, type Unit } from "@/components/ds/unit";
import { baseValue, periodNet, periodNetCoverage, periodNetLabel } from "./periodNet";
import { useSleeves } from "@/lib/framework/useSleeves";
import { useLaneCadence, type LaneCadence } from "@/lib/framework/useLaneCadence";
import { useLaneMarketAware, type LaneMarketAware } from "@/lib/framework/useLaneMarketAware";
import { measuredUnrealizedDelta, netFromComponents } from "./panelIdentity";
import type { AccountDTO, TradeDTO } from "@/lib/api/types";
import type { TileProps } from "@/lib/framework/types";
import type { BookConfig } from "./definition";

function tint(n: number): string {
  return n > 0 ? "text-status-bull" : n < 0 ? "text-status-bear" : "text-t3";
}

/** One side of the split. Colour is never alone — label and held count carry the meaning too. */
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
  cadence,
  marketAware,
}: {
  label: string;
  book: Book;
  /** The GLOBAL selector, which the HEADLINE now answers (#699). */
  period: string;
  day?: DayMove;
  /** Swept realized for the SELECTED window, or null when the sweep has no answer yet (#345 item 3). */
  periodRealized: number | null;
  /**
   * The window's change in this lane's mark (#699/#734), computed by the PARENT because it needs the
   * fetched base map. CARRIED beside the headline, never summed into it — see `cellHeadline`.
   * `null` is unknown: no base captured, the read failed, or an unpriced leg at the base date.
   */
  unrealizedDelta?: number | null;
  /**
   * The lane's window NET OF FLOWS (#699 a), computed by the PARENT from the fetched `net` terms and
   * this lane's live market value. THE HEADLINE for every window but 1D when known; unknown falls
   * back to window realized. See `windowNet` and `cellHeadline`.
   */
  net?: WindowNet | null;
  periodLabel: string;
  /** The unit this cell renders relative changes in (#586) — the global toggle's state. */
  unit: Unit;
  /** THIS LANE'S sleeve, the denominator for its percentages. `null`/absent renders `—` rather than
   *  falling back to the account, which would answer a different question under the same label. */
  sleeve?: number | null;
  /** THIS LANE'S cadence (#888). Absent means the read failed and renders `cadence —`, never as daily. */
  cadence?: LaneCadence;
  /** THIS LANE'S market-aware readings (#873). Absent/null renders `market view —`, never as clear. */
  marketAware?: LaneMarketAware | null;
}) {
  // ONE RULE, TWO TILES (#699). `cellHeadline` decides what the largest number in this cell means and
  // ManagedPortfolioTile's cell asks the same question of the same helper — #523 cost three separate
  // defects precisely because these two carried two copies of one rule.
  const head = cellHeadline(period, day, periodRealized, unrealizedDelta, net);
  // SAME RULE AS THE PANEL ABOVE, against this lane's own sleeve (#586). The decision itself lives in
  // `laneFigure` because the OTHER lane cell needs the same three lines and a second copy is how the
  // two tiles came to render one value in two units (#392).
  const cellUnit = (v: number | null): string => laneFigure(v, unit, sleeve);
    return (
    <div className="bg-surf px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-t3">{label}</div>
      <div
        className={`font-mono text-base ${head.value === null ? "text-t3" : tint(head.value)}`}
        title={
          head.kind === "day"
            ? "Today's move on what this lane holds: qty x (mark - prior close). The one window with a true per-lane delta."
            : head.kind === "net"
              ? `ΔMV − flows over ${periodLabel}: the change in this lane's market value net of what it invested (buys − sells) — the same identity as DELTA NET above, with no cost basis in it. Realized (FIFO) and the change in mark ride in the small print.${head.partial ? ` PARTIAL — ${head.partial}.` : ""}`
            : head.kind === "unswept"
              ? `The ${periodLabel} window has not been swept yet — this is unknown, not zero.`
              : `REALIZED over ${periodLabel} — money from positions CLOSED in the window. Not the same composition as the hero above, which also carries the change in unrealized; the window's net of flows is unknown here: ${net?.reason ?? "no net terms were fetched"} (#699).`
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
      {/* THREE CASES, NOT TWO (#345 item 6). This gated on `day.covered > 0` and rendered nothing
          otherwise, so "holds nothing", "prior closes never arrived" and "no figure at all" were
          indistinguishable — and only the first is benign. Prior closes ride the `today_ranges`
          websocket plane, and the engine tombstones a stale snapshot, so a feed gap zeroes `covered` for
          every strategy at once and the line vanishes from the whole panel. #298: silence must be
          distinguishable from absence. */}
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
        <div className={`font-mono text-[10px] ${tint(head.day.value)}`}>
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
        {book.held} held · {head.kind === "day" ? "day" : head.kind === "net" ? `net · ${periodLabel}` : `real · ${periodLabel}`}
        {/* THE STANDING LEVEL IS DEMOTED, NOT DROPPED (#699). It answers "what is this lane's book
            worth right now", which the window figure does not — it was only ever wrong as the
            HEADLINE, under a selector it does not obey. */}
        {/* UNREALIZED — the same quantity the header calls "standing" (#808 item 1). This read
            `book.total` (realized-on-open-cycles + unrealized) while the header summed unrealized
            only; on 2026-09-09 the lanes summed to 3,110.69 under a header of 3,048.20, the 62.49 gap
            being exactly Σ open-cycle partial realized. One word, one quantity. */}
        {` — standing ${fmtUsd(book.unrealized)}`}
        {/* THE WINDOW'S CHANGE IN MARK (#699/#734) — its OWN figure, NEVER added to the realized
            headline. `realized(W)` is FIFO by lot while both ends of this are average cost, so a
            partial close of heterogeneous lots makes the sum wrong by
            `closed_qty x (avg - fifo_lot_basis)`; measured, and momentum scales out routinely.
            Two labelled numbers beat one confident wrong one.
            OMITTED WHEN UNKNOWN rather than shown as zero: no base captured for that window, an
            unpriced leg at the base date, or the read failed. A "mark +$0.00" where the truth is
            "we never observed that day" is the confident wrong number this whole table exists to
            replace with an honest silence. */}
        {/* READ FROM `head`, NOT FROM THE PROP. Rendering the raw prop bypassed `cellHeadline`
            entirely — including its non-finite refusal — so a NaN would have reached the screen and
            the helper's 4th argument was dead: a mutant deleting it left every test green. Two paths
            for one value, which is the drift this whole chain is about. One source. */}
        {typeof head.unrealizedDelta === "number" && (
          <span title={`Change in this lane's unrealized over ${periodLabel}, from the end-of-day `
            + `position record. Shown beside realized rather than added to it: the two use different `
            + `cost bases, so summing them is wrong on a partial close.`}>
            {" · mark "}
            <span className={tint(head.unrealizedDelta)}>{fmtUsd(head.unrealizedDelta)}</span>
            {` · ${periodLabel}`}
          </span>
        )}
        {head.kind === "window" || head.kind === "unswept" ? null : (
          <>
            {" · real "}
            {periodRealized === null ? (
              <span title={`No broker sweep for ${periodLabel} yet — unknown, not zero`}>—</span>
            ) : (
              cellUnit(periodRealized)
            )}
            {` · ${periodLabel}`}
          </>
        )}
        {/* THE CADENCE BESIDE THE LIVENESS ALARM (#888). A monthly lane says so and names its next
            rebalance; a daily one says nothing; a lane the fetch could not describe says `cadence —`
            rather than passing for daily — the reading that took QC345-003 for a dead lane. */}
        {(() => {
          const note = cadenceNote(cadence);
          return note === null ? null : <span title={note.title}>{` · ${note.text}`}</span>;
        })()}
        {/* THE MARKET VIEW BESIDE IT (#873 phase 1). Three states outside and inside; `null`, unknown,
            fault and a contract absent from the pin each render as themselves, never as clear. */}
        {(() => {
          const badge = marketAwareBadge(marketAware);
          return <span className={badge.tone} title={badge.title}>{` · ${badge.text}`}</span>;
        })()}
      </div>
    </div>
  );
}

/** One hero cell. `title` carries the reason when a value is absent or partial by design. */
function Metric({
  label,
  value,
  cls,
  title,
  suffix,
  sub,
}: {
  label: string;
  value: string;
  cls: string;
  title?: string;
  /** "+" marks a figure known to be understated (positions held without a mark). */
  suffix?: string;
  sub?: string;
}) {
  return (
    <div className="bg-surf px-3 py-2" title={title}>
      <div className="text-[10px] uppercase tracking-wider text-t3">{label}</div>
      <div className={`font-mono text-sm ${cls}`}>
        {value}
        {suffix && <span className="text-t3">{suffix}</span>}
      </div>
      {sub && <div className="font-mono text-[10px] text-t3">{sub}</div>}
    </div>
  );
}

interface ExternalItem {
  source: string;
  realized_pnl?: string | null;
  unrealized_pl?: number | null;
  market_value?: number | null;
}

export function BookTile({ data, status: srcStatus }: TileProps<BookConfig>) {
  // Prior closes ride the shared `today_ranges` plane — the same source the watchlist computes chg% from,
  // so a name's day change reads identically here and there.
  const priorCloses = useTodayRanges();
  // #437: the same list the banner reads. `held` below counts CYCLE ROWS, and a mis-attributed lane
  // makes that count describe bookkeeping rather than ownership — on 2026-08-30 it read 38 while the
  // broker held 20. The row count is not corrected here (we cannot know the true split from this
  // plane); it is QUALIFIED, so it stops being read as "positions you own".
  const { ownershipViolations, unpricedPositions } = useHealth(Date.now());
  const disputed = disputedBadge(ownershipViolations);   // three-state (#884): unknown ≠ none
  // The per-lane window base (#699/#734). Its own query: it reads a database and its answer changes
  // once a day, so it must not ride the continuously-pushed trade frame.
  const windowBase = useWindowBase();
  const tradesFrame = data.trades as {
    trades?: TradeDTO[];
    status?: string;
    error?: string | null;
    realized_session?: SessionRealizedLike | null;
    // EVERY KEY THE ENGINE PUBLISHES MUST BE DECLARED HERE. A narrow local type is the TypeScript form
    // of the Pydantic-drops-a-field defect that has now shipped three times (#233 / #322 / #336): the
    // engine publishes it, the declaration omits it, and the tile reads `undefined` forever while
    // everything compiles. `by_strategy` and `unclaimed` are #345 item 3.
    realized_periods?: Record<
      string,
      {
        total?: number | null;
        unmatched?: number | null;
        by_strategy?: Record<string, number> | null;
        unclaimed?: number | null;
        /** Oldest closed leg the engine holds (#846) — how far back this window can see. */
        horizon_ts?: number | null;
        /** Why this window has no number, when it has none (#846) — a refusal, never a zero. */
        error?: string | null;
      } | null
    > | null;
    /** The broker fill sweep, beside the legs (#846). Alpaca only; null on IBKR. `net` = fills + cash adjustments. */
    realized_periods_swept?: Record<string, { total?: number | null; net?: number | null } | null> | null;
  } | undefined;
  const trades = (tradesFrame?.trades ?? []).filter((t) => t.is_engaged);
  const account = (data.account as { account?: AccountDTO | null } | undefined)?.account ?? undefined;
  const external = ((data.external_activity as { external?: ExternalItem[] } | undefined)?.external ?? []).filter(
    (x) => x.source === "POSITION",
  );

  const books = attribute(trades, external);
  const allBooks = [...books.strategies.map((s) => s.book), books.unclaimed];
  const total = allBooks.reduce((s, b) => s + b.total, 0);
  // Unknown rolls up: a total quietly missing one book's unrealized is worse than one that says so.
  const unknown = allBooks.reduce((s, b) => s + b.unknown, 0);
  const held = allBooks.reduce((s, b) => s + b.held, 0);
  const equity = account?.equity ?? null;
  const cash = account?.cash ?? null;
  // REALIZED comes from the ENGINE, not from summing the live cycles (#233).
  //
  // A CLOSED cycle is emitted once and dropped from the projection, so `allBooks` only ever contains
  // live ones — summing them made this structurally $0.00 the moment a position closed. The operator saw the
  // tile read $0.00 on a day four MANUAL positions had been closed for +$483.
  //
  // The engine answers it from native closed positions, which is the only place the fact survives. The
  // old sum stays as the fallback for a frame that predates the field, and for a node with no engine.
  // REALIZED answers for the GLOBAL period (#322), not always for today. Operator: equity had moved ~$2,841
  // over the week while this read $1,061, because the session figure can only see positions closed since
  // the engine last started. The swept figure comes from the broker fills and survives restarts.
  //
  // `null` means NOT SWEPT YET and renders as a dash — a zero here would claim a whole week closed flat.
  const period = useCockpitStore((st) => st.period);
  const unit = useCockpitStore((st) => st.unit);
  const sleeves = useSleeves();
  const laneCadence = useLaneCadence();
  const laneMarketAware = useLaneMarketAware();
  // ROWS AFTER `period`, because the row list depends on the selected window. A strategy that is FLAT
  // has no live trade cycle and therefore no book, so without the sweep's key set it has no row and
  // its realized vanishes with it (#523) — on 2026-08-24 MANUAL-001 was the largest realized
  // contributor in the book (+1522.07 over 1M) and was not on the panel at all.
  // The third argument is the swept unclaimed figure. THIS TILE HAD THE SAME HOLE: the row list is
  // built by the shared `visibleBooks`, so with no live unclaimed position EXTERNAL's swept money was
  // filtered out here too and landed nowhere (#596, one row over).
  const rows = visibleBooks(
    books,
    periodRealizedByStrategy(tradesFrame, period),
    rowPeriodRealized(tradesFrame, period, "Unclaimed"),
  );
  const periodLabel = PERIODS.find(([k]) => k === period)?.[1] ?? period;
  // Σ OF THE LANES' OWN MARK DELTAS (#808 item 3) — the SAME derivation each cell renders
  // (`windowDelta` per row), summed once here so the header can name it beside the broker's Δ.
  // null when no lane has a known delta: unknown, not zero.
  const laneMarkDeltas = rows.map((r) => windowDelta(r.book.unrealized, windowBase?.by_period?.[period], r.label));
  const laneMarksKnown = laneMarkDeltas.filter((d): d is number => typeof d === "number");
  const laneMarksSum = laneMarksKnown.length > 0 ? laneMarksKnown.reduce((s, d) => s + d, 0) : null;
  // A Σ over SOME lanes is labelled as such — "(3/5)" — never presented as the whole (codex, #808).
  const laneMarksCoverage = laneMarksKnown.length < laneMarkDeltas.length ? ` (${laneMarksKnown.length}/${laneMarkDeltas.length})` : "";
  // NET for the selected window (#336) — the BROKER's own P&L for the period, which is the same fact the
  // EQUITY header states. Declared after `period` (it reads it) and deliberately NOT from `total`, whose
  // session-realized component froze the hero number across every window.
  // NET IS A SUM OF TWO MEASURED TERMS (#596). Operator: "Net at the top is simply realised +
  // unrealised for the period. cash and liquidation and balance are completely different things."
  //
  // It used to BE `periodNet` (equity - base_value), with Δunrealized back-solved as `net - realized`
  // — so the identity the panel displays held by construction and could never disagree. On
  // 2026-08-27 every period rendered Δ = exactly minus REALIZED, and NET $0.00.
  // `periodNet` (equity - base_value) IS NO LONGER THE SOURCE and is deliberately NOT computed here.
  //
  // The plan was to keep it as an independent check — two derivations of one fact, so a
  // disagreement becomes a detector. It is left OUT rather than assigned and ignored, because a
  // variable nothing reads is a detector that does not detect, and reads on review as though the
  // check exists (codex, implementation review). Wiring it up is #596's follow-up.
  //
  // Whoever does that must expect a KNOWN gap on windowed periods rather than chase it: realized is
  // recognised at the SALE with the lot's original basis (`realized_broker.py:126`), so a lot bought
  // before a window and sold inside it puts its whole lifetime gain in that window, while
  // Δunrealized covers only what is still open. ALL is exact; nothing precedes inception.
  const unclaimedRealized = periodUnclaimedRealized(tradesFrame, period);
  const realized = periodRealizedOf(tradesFrame, period, allBooks.reduce((s, b) => s + b.realized, 0));
  // A window can UNDERSTATE, badly, and silently. `unmatched` counts sells whose opening buy falls
  // outside it — their basis is unknowable from the window's fills, so they contribute zero. That is
  // why 1W can read higher than All (live 2026-08-17: $2,195.32 with 19 unmatched vs $107.70 with
  // none). Both figures are right; a panel that shows them without saying so looks broken.
  // NET, WITH THE MEASURED TERM PREFERRED AND THE CURVE AS THE FALLBACK (#596).
  //
  // Operator, 2026-08-27: "i have no stats. it does not help. cannot run like this." An em dash on
  // 1W/1M/3M is as useless as the $0.00 it replaced — the panel exists to be read. Where the broker
  // measures the window's mark movement we use it, and NET is a true sum of two independent figures.
  // Where it does not, NET is the account's own equity change over the window (`equity - base_value`),
  // which is ALSO measured — two broker snapshots — and Δ becomes the residual of those two.
  //
  // WHAT CHANGED FROM THE VERSION THIS REPLACED is which number is load-bearing, and whether the
  // panel admits it. Before, Δ was ALWAYS `net - realized` while both were shown as measurements, so
  // the stated identity held by construction and could never disagree — on 2026-08-27 every period
  // printed Δ as exactly minus REALIZED with NET $0.00. Now the measured path wins where it exists,
  // the derived path is labelled, and on 1D the two agree to the cent with Alpaca's own
  // `equity - last_equity` (913.01 both ways, measured).
  const measuredDelta = measuredUnrealizedDelta(period, account as never);
  const curveNet = periodNet(data.equity_curve as Parameters<typeof periodNet>[0], period, account);
  const netMeasured = netFromComponents(realized, measuredDelta);
  const net = netMeasured !== null ? netMeasured : curveNet;
  // Residual, not a measurement, whenever the broker gave no window figure. Same arithmetic the old
  // code used; the honesty is the label beside it, not withholding the number.
  const dUnrealized = measuredDelta !== null
    ? measuredDelta
    : (net !== null && realized !== null ? net - realized : null);
  const dUnrealizedIsDerived = measuredDelta === null;
  const partial = periodRealizedPartial(tradesFrame, period);
  // SAY HOW FAR BACK, AND WHAT THE BROKER SAYS (#846). The figure comes from Nautilus's own closed legs
  // on every venue; `all` is the oldest leg this cache holds, not inception. Where a broker sweep
  // exists its total is shown beside — it carries fees and withholding the legs cannot see, and it
  // reaches inception — never averaged in. A truncated number under the label "all" with no horizon
  // is degrading quietly (scope review, #846).
  const horizonTs = periodHorizonTs(tradesFrame, period);
  const horizonLabel = horizonTs === null || period === "1D"
    ? null
    : `since ${new Date(horizonTs / 1_000_000).toLocaleDateString("en-CA", { timeZone: "America/New_York" })}`;
  const brokerRealized = periodBrokerRealized(tradesFrame, period);
  const unrealized = allBooks.reduce((s, b) => s + b.unrealized, 0);
  // The window's change in the mark — the term NET is actually built from (#345). Declared here because
  // it reads `net` and `realized` above; it is DERIVED from them, never recomputed from per-position
  // marks, so the panel cannot contradict its own hero number.
  // DEPLOYED comes from the BROKER when it offers the figure (#310). The operator added the panel up and it did
  // not reconcile: DEPLOYED + CASH = 99,769.90 against a LIQUIDATION of 99,764.62, because DEPLOYED was
  // derived from OUR marks while CASH and LIQUIDATION came from Alpaca. Alpaca's own numbers satisfy
  // `cash + long_market_value == equity` to the cent, so taking all three from one snapshot makes the
  // panel add up by construction. The projection-derived figure stays as the fallback for a node with no
  // broker account, and it is the one that can under-count when a position has no mark.
  const derived = deployedValue(trades, external);
  const brokerLmv = (account as { long_market_value?: number } | undefined)?.long_market_value;
  const deployed =
    brokerLmv != null && brokerLmv > 0 ? { value: brokerLmv, unmarked: 0 } : derived;
  // THE ENGINE'S OWN LIST, passed in rather than re-derived here. It knows what it could not price
  // because it TRIED; a second derivation in the UI is how two answers to one question drift apart,
  // which this file's `isProtected` comment already pays for.
  const secured = securedValue(trades, unpricedPositions);
  // ONE clock for both, captured here. Each call defaulting to its own `Date.now()` lets a render that
  // straddles ET midnight apply a different same-day rule to the aggregate than to the per-strategy
  // cells, so the parts would not sum to the whole. (codex review, Low.)
  const nowMs = Date.now();
  const feed = feedState(tradesFrame, trades.length > 0 || external.length > 0);
  const day = dayMove(trades, priorCloses, nowMs);
  const dayByStrategy = dayMoveByStrategy(trades, priorCloses, nowMs);
  // % of LIQUIDATION, not of cash — "how much of the account is at risk" is the question this answers.
  const pctDeployed = equity && equity > 0 ? (deployed.value / equity) * 100 : null;
  // THE UNIT SWITCH (#586). `windowBase` is equity at the START of the selected window, taken from
  // `baseValue` — the same derivation NET is built from — so a percentage cannot disagree with the
  // dollar figure it replaces.
  //
  // Three figures share this base on purpose: NET, REALIZED and Δ UNREALIZED. The panel states the
  // identity "NET = REALIZED + Δ", and three shares of ONE denominator still sum, so the identity
  // survives the switch instead of becoming arithmetic nonsense in `%` mode.
  // NAMED FOR WHAT IT IS, and deliberately NOT reusing `windowBase` from `useWindowBase()` above.
  // That one is the per-lane UNREALIZED base (`{by_period, unreadable}`); this is the ACCOUNT's
  // equity at the window's start. Two different facts — the first draft shadowed the hook's value
  // and silently inherited its shape, which typecheck caught.
  const windowBaseEquity = baseValue(
    (data.equity_curve as { curves?: Record<string, unknown> } | undefined)?.curves?.[period] as never,
    period,
    account as never,
  );
  /** The denominator #586 decided for a figure, read from `BOOK_FIGURES` and never chosen inline —
   *  the table is where the decision lives, and a denominator picked at a call site is how a panel
   *  drifts from the ticket that decided it. */
  const denominatorFor = (figure: keyof typeof BOOK_FIGURES): number | null =>
    BOOK_FIGURES[figure] === "windowBase" ? windowBaseEquity
      : BOOK_FIGURES[figure] === "deployed" ? deployed.value
      : BOOK_FIGURES[figure] === "equity" ? equity
      : null;
  /** Render a figure in the selected unit, or `—` when no honest percentage exists.
   *
   * REFUSES, LIKE THE LANE CELL DOES (#1077). This used to fall back to `fmtUsd` when `percentOf`
   * returned null, on the reasoning that the dollar form is "the honest answer, not a guess". It is
   * honest about the AMOUNT and dishonest about the UNIT: the reader selected `%`, and the panel then
   * printed `-$57.47` beside figures that had switched, with nothing saying which was which. That is
   * #392's own complaint class — a field that silently declines to switch is the same failure as one
   * that never could — one panel up from where #1076 had just fixed it. `laneFigure`, in the cell
   * below, already refused; the two renderers now state one rule, and `unitRefusal` puts the reason
   * on the title beside the dash so `—` is a statement, not a blank.
   */
  const inUnit = (value: number | null, figure: keyof typeof BOOK_FIGURES): string => {
    if (value === null) return "—";
    if (unit === "$" || BOOK_FIGURES[figure] === "none") return fmtUsd(value);
    const pct = percentOf(value, denominatorFor(figure));
    return pct === null ? "—" : fmtPct(pct);
  };
  /** Why `inUnit` would refuse a figure right now, for the title beside its dash — or null when it
   *  would not. Asked of the DENOMINATOR, not of a value, so the reason is the same for every figure
   *  that shares one, and so a `—` that means "unknown value" is not mislabelled as a unit refusal. */
  const unitRefusal = (figure: keyof typeof BOOK_FIGURES): string | null => {
    const short = unitRefusalShort(figure);
    return short === null ? null : `${short} — shown as — rather than as a dollar figure under a % toggle (#1077). Switch to $ for the amount.`;
  };
  /** The SHORT form of the refusal, for the VISIBLE sub-line (peer review, S1). Every "the dash says
   *  why" above is a `title`, which is hover-only — and #392 was reported FROM THE PHONE, where a
   *  title never renders. Pre-fix the phone showed a wrong-unit number; a title-only refusal would
   *  have it show an unexplained blank instead. The reason has to be on the screen, not behind it. */
  const unitRefusalShort = (figure: keyof typeof BOOK_FIGURES): string | null => {
    if (unit === "$" || BOOK_FIGURES[figure] === "none") return null;
    if (percentOf(1, denominatorFor(figure)) !== null) return null;
    // `deployed` IS zero while positions are held in exactly one state: none of them has a mark
    // (`deployedValue` skips a position with no market_value/last_px and counts it in `unmarked`).
    // "Nothing deployed" would then contradict the Deployed cell beside it, which reads
    // `$0.00+ · N without a mark`. Say what is actually missing (peer review, point 2).
    return BOOK_FIGURES[figure] === "windowBase" ? `no ${periodLabel} base to take % against`
      : BOOK_FIGURES[figure] === "deployed"
        ? deployed.unmarked > 0 ? `deployed value unknown — ${deployed.unmarked} without a mark` : "nothing deployed to take % against"
      : "no account equity to take % against";
  };
  // The broker's figure needs the unit formatter, which is declared just above.
  // OMITTED WHEN REFUSED (#1077): `broker —` beside a realized `—` names a disagreement that cannot be
  // read, and the tooltip above already says why nothing is shown.
  const brokerLabel = brokerRealized !== null && realized !== null && Math.abs(brokerRealized - realized) > 0.005 && unitRefusal("realized") === null
    ? `broker ${inUnit(brokerRealized, "realized")}`
    : null;

  return (
    <div>
      <div className="mb-3 flex items-baseline justify-between">
        {/* NO UnitToggle here. The Board renders the global one on the period row, opposite the
            period selector, and this tile reads the same store — the same arrangement MarketTile
            already documents for the selector itself. */}
        <h2 className="text-sm font-semibold uppercase tracking-wider text-t2">Book</h2>
        <span className="font-mono text-xs text-t3">
          {held} held
          {disputed.kind !== "none" && (
            <span
              className={disputed.kind === "disputed" ? "ml-2 text-status-bear" : "ml-2 text-status-watch"}
              title={disputed.title}
            >
              {disputed.label}
            </span>
          )}
          {books.unclaimed.phantom > 0 && (
            <span
              className="text-status-bear"
              title={
                `${books.unclaimed.phantom} UNCLAIMED row${books.unclaimed.phantom > 1 ? "s" : ""} the broker holds NONE of — ` +
                `reconciliation-minted counterparties (#807). Excluded from every figure on this panel; ` +
                `listed on the Portfolio tab as phantoms.`
              }
            >
              · {books.unclaimed.phantom} phantom
            </span>
          )}
        </span>
      </div>

      {/* The engine reports which kind of empty this is (#298). Without it, a broken projection, a
          reconciling startup and a genuinely flat book all rendered as the same blank tile — and an empty
          book is indistinguishable from a liquidated one. */}
      {feed.banner && (
        <div className="mb-2 rounded-lg border border-status-bear/40 bg-status-bear/15 px-2 py-1 font-mono text-[10px] text-status-bear">
          {feed.banner}
        </div>
      )}
      {feed.blockingMessage ? (
        <div className="rounded-xl border border-dashed border-ds-line py-12 text-center font-mono text-sm text-t3">
          {feed.blockingMessage}
        </div>
      ) : (
      <TileState
        status={feed.healthy ? srcStatus.trades : "stale"}
        isEmpty={trades.length === 0 && external.length === 0}
        emptyLabel="Nothing held"
      >
        {/* NET answers for the SELECTED PERIOD (#336). It used to be `Σ book.total` — period realized plus
            the whole standing unrealized — which froze at $1,705.31 across 1D/1W/1M/3M while REALIZED
            underneath it moved, because the session realized it summed reads $0.00 after any restart.
            Now it is the broker's own P&L for the window, the same fact the EQUITY header states, so the
            two cannot disagree. `null` renders "—": an unknown window must never fall back to unrealized,
            which is precisely the bug. */}
        <div className="mb-3">
          <div className="text-[10px] uppercase tracking-widest text-t3">{periodNetLabel(period)}</div>
          <div
            className={`font-mono text-2xl font-semibold ${net === null ? "text-t3" : tint(net)}`}
            title={net === null ? undefined : (unitRefusal("net") ?? undefined)}
          >
            {inUnit(net, "net")}
          </div>
          <div className="text-[10px] text-t3">
            {(() => {
              if (net === null) return `No broker P&L for ${periodLabel} yet — unknown, not zero`;
              // THE REFUSAL IS VISIBLE, not hover-only (#1077, S1): a `—` above "realized + change in
              // unrealized … per the broker" reads as a broken tile on a phone.
              const refused = unitRefusalShort("net");
              if (refused !== null) return `${refused} — switch to $`;
              // A clamped window must SAY so (#653): on a young account the 3M base is inception, so
              // the delta is true but a bare "3M" overstates the span it covers.
              const cov = periodNetCoverage(data.equity_curve as Parameters<typeof periodNetCoverage>[0], period);
              return cov && !cov.covered
                ? `covers ${cov.coversDays ?? "?"}d of ${periodLabel} — account younger than the window`
                : "realized + change in unrealized over the window, per the broker";
            })()}
          </div>
        </div>

        {/* The hero grid from the Option A mock. `secured` is computed live from resting protective
            orders — it reads $0.00 today because MOMENTUM protects by decision rather than by an order
            at the venue, and the sub-line says how many holdings that leaves unprotected. */}
        <div className="mb-3 grid grid-cols-2 gap-px overflow-hidden rounded-md border border-line bg-line sm:grid-cols-3">
          {/* DAY removed (#336). With NET following the selector, DAY *is* NET at 1D — Operator: "why do we
              have separate day". It also answered a subtly different question (today's move on what is
              still HELD, excluding anything closed today), which sitting unlabelled beside a period NET
              made the panel read as self-contradictory. `dayMove` is still computed for the per-strategy
              rows below, where the held-only reading is the one wanted. */}
          <Metric
            label={`Realized ${periodLabel}`}
            value={realized === null ? "—" : `${inUnit(realized, "realized")}${partial.partial ? "*" : ""}`}
            cls={realized === null ? "" : tint(realized)}
            sub={[
              unitRefusalShort("realized"),
              partial.partial ? `+${partial.unmatched} partial on open` : null,
              horizonLabel,
              brokerLabel,
            ].filter(Boolean).join(" · ") || undefined}
            title={
              realized === null
                ? `No realized figure for ${periodLabel} yet — unknown, not zero`
                : [
                    unitRefusal("realized"),
                    `Realized P&L over ${periodLabel} from Nautilus's own closed position legs — every close, restart-safe, on every venue (#846).`,
                    partial.partial ? `UNDERSTATED: ${partial.unmatched} still-open position${partial.unmatched === 1 ? " has" : "s have"} a partial exit whose P&L native cannot date, so it is counted here rather than folded in.` : null,
                    horizonLabel ? `This cache's oldest leg is from ${horizonLabel.slice(6)} — the window cannot see before it; "All" is not account inception.` : null,
                    brokerRealized !== null && unitRefusal("realized") === null ? `The broker's own ledger says ${inUnit(brokerRealized, "realized")} net for the same window — fills plus the fees and withholding the legs cannot see, back to account inception. Shown, not averaged.` : null,
                  ].filter(Boolean).join(" ")
            }
          />
          {/* Δ UNREALIZED, not the standing level (#345). NET's subtitle promises "realized + change in
              unrealized over the window" and this cell used to render the whole standing mark, so adding
              the two terms the subtitle names overshot NET by the unrealized accrued BEFORE the window —
              1W read 2,650.21 + 1,176.79 = 3,827.00 against a NET of 2,922.86. The operator added them up and
              asked why. Derived as NET − REALIZED rather than recomputed from marks, so it cannot
              disagree with the hero number above it. The standing level keeps the sub-line.

              WHAT THE CELLS BELOW SUM TO CHANGED IN #699. They used to lead with the standing level,
              so this sub-line named their total; they now lead with REALIZED for the window, and the
              standing level is a sub-line down there too. Corrected here in the same change, because
              a sentence that describes the old value is the dangerous kind of wrong — it survives
              review by sounding careful. */}
          <Metric
            label={`Δ Unrealized ${periodLabel}`}
            value={inUnit(dUnrealized, "dUnrealized")}
            cls={dUnrealized === null ? "" : tint(dUnrealized)}
            sub={`${unitRefusalShort("dUnrealized") ? `${unitRefusalShort("dUnrealized")} · ` : ""}${fmtUsd(unrealized)} standing${laneMarksSum === null ? "" : ` · lane marks Σ ${fmtUsd(laneMarksSum)}${laneMarksCoverage}`}`}
            title={
              dUnrealized === null
                ? `NET or REALIZED is unknown for ${periodLabel}, so the window's change in the mark cannot be derived — unknown, not zero.`
                : `${unitRefusal("dUnrealized") ? `${unitRefusal("dUnrealized")} ` : ""}How much of NET came from the mark MOVING across ${periodLabel}, rather than from trades closed in it: NET − REALIZED — the BROKER's equity residual. The sub-line is the whole standing unrealized on what is held right now (gain accrued BEFORE this window included), and, when bases exist, Σ of the lanes' own "mark" figures — OUR marks against the end-of-day base. Two derivations of one window; they differ by basis noise and by anything the broker counts that our marks do not. The per-strategy cells lead with REALIZED for the window (#699), so they sum toward REALIZED, not toward this figure.`
            }
          />
          <Metric
            label="Secured · now"
            value={inUnit(secured.value, "secured")}
            // A REFUSED dash is not a bull figure (peer review): colour follows what is printed.
            cls={secured.value > 0 && unitRefusal("secured") === null ? "text-status-bull" : "text-t3"}
            // $0.00 MEANS TWO DIFFERENT THINGS AND THE SUB-LINE HAS TO SAY WHICH (#345). The line used to
            // render only when something was unprotected, so a fully-protected book showed a bare
            // "$0.00" — which reads as "nothing is protected" when it actually means "no stop sits above
            // its entry yet". Live on 2026-08-20: 3 of 3 protected, secured $0.00, and nothing on screen
            // separated those. A zero in a safety slot must state its own reason.
            // ONE DERIVATION, shared with `title` below (#786). The inline ternary this replaced
            // fell through on `covered > 0` and never inspected `value`, so a book with $925.85
            // secured still read "none above entry" — a safety cell contradicting its own number.
            sub={unitRefusalShort("secured") ? [unitRefusalShort("secured"), securedSub(secured, held)].filter(Boolean).join(" · ") : securedSub(secured, held)}
            title={
              unitRefusal("secured")
                ? `${unitRefusal("secured")} ${securedSub(secured, held) ?? ""}`.trim()
                : secured.unprotectable > 0
                ? `${secured.unprotectable} held position${secured.unprotectable > 1 ? "s" : ""} the engine cannot PRICE, so protection cannot be sized for ${secured.unprotectable > 1 ? "them" : "it"} at all. This does not drain the way an unprotected backlog does — it lasts as long as the venue withholds market data.`
                : secured.naked > 0
                ? `${secured.naked} held position${secured.naked > 1 ? "s" : ""} with no protective stop resting — nothing locked in there, whatever the mark says. MOMENTUM exits are rule-based per session, so it protects by decision rather than by an order at the venue.`
                : noneAboveEntry(secured)
                  ? "Every holding has a protective stop resting, but none of them sits above its entry — so there is no locked-in gain yet. Protected and secured are different questions; this cell answers the second."
                  : "Gain a resting protective stop would realise if it triggered now — the part of unrealized that cannot evaporate"
            }
          />
          <Metric
            label="Deployed · now"
            value={fmtUsd(deployed.value)}
            cls="text-t1"
            suffix={deployed.unmarked > 0 ? "+" : undefined}
            sub={pctDeployed != null ? `${pctDeployed.toFixed(0)}% of liq` : undefined}
            title={
              deployed.unmarked > 0
                ? `${deployed.unmarked} held position${deployed.unmarked > 1 ? "s" : ""} without a mark — worth more than shown`
                : "Market value of everything held"
            }
          />
          <Metric label="Cash · now" value={cash != null ? fmtUsd(cash) : "—"} cls="text-t1" />
          <Metric
            label="Liquidation · now"
            value={equity != null ? fmtUsd(equity) : "—"}
            cls="text-t1"
            title="Net liquidation value — the broker's own equity figure (cash + market value of holdings), not derived here"
          />
        </div>

        {/* One cell per StrategyId that has something to say, unclaimed last. Empty when a single book
            would show — the breakdown would just restate the net above it. */}
        {rows.length > 0 && (
          <div className="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-line bg-line sm:grid-cols-3">
            {rows.map((r) => (
              <BookCell
                key={r.label}
                label={r.label}
                book={r.book}
                period={period}
                unit={unit}
                // `r.label` IS the strategy id, which is the key `/strategies` returns.
                sleeve={sleeves[r.label]}
                cadence={laneCadence[r.label]}
                marketAware={laneMarketAware[r.label]}
                day={dayByStrategy.get(r.label)}
                // THE SELECTOR NOW REACHES THE CELLS (#345 item 3). `rows.map` never read `period`, so
                // the breakdown could not respond to it at all and showed one fixed figure under every
                // tab. `r.label` IS the strategy id, which is the key the engine buckets under.
                // THE UNCLAIMED ROW IS BUCKETED UNDER "EXTERNAL", not under its own label, so
                // looking it up by `r.label` returned the "genuine zero" branch and -1,093.30
                // disappeared from a panel whose job is to add up (#596).
                // ONE derivation, shared with ManagedPortfolioTile (`rowPeriodRealized`). This ternary
                // used to live here and nowhere else; the other panel had no swept figure at all, so
                // the two tiles answered the same question differently. Behaviour is unchanged — the
                // helper IS this expression, moved so there is only one of it.
                periodRealized={rowPeriodRealized(tradesFrame, period, r.label)}
                unrealizedDelta={windowDelta(r.book.unrealized, windowBase?.by_period?.[period], r.label)}
                net={windowNet(r.book.marketValue, windowBase?.net?.[period], r.label)}
                periodLabel={periodLabel}
              />
            ))}
          </div>
        )}
        {/* WHAT THE BREAKDOWN DOES NOT ACCOUNT FOR. Measured 2026-08-21: fills join to a cached order
            100% of the time from 2026-08-17 and ~0% before it, so on `all` the unattributed share is
            LARGER than everything above it. A breakdown that omitted it would look complete while
            covering a fraction of the money — and per #292 it must not be distributed to whoever closed
            the position. Shown only when non-zero: on 1D it is genuinely 0 and the line would be noise. */}
        {rows.length > 0 && (unclaimedRealized ?? 0) !== 0 && (
          <div className="mt-1 px-1 font-mono text-[10px] text-t3">
            unattributed {fmtUsd(unclaimedRealized as number)} · {periodLabel}
            <span title="Realized whose opening lot predates the order cache, or was not opened by this cockpit. P&L follows the OPENING strategy (#292), so it is reported here rather than credited to whoever closed the position.">
              {" "}?
            </span>
          </div>
        )}
      </TileState>
      )}
    </div>
  );
}

export { money };
