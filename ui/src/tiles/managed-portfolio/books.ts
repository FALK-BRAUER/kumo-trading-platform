/**
 * Per-strategy P&L attribution (#212 / #68 / #72, PR #223 Option A).
 *
 * Pure — no JSX, no React — so it is testable in isolation, same shape as `components/ds/pnl.ts`.
 *
 * One total across the whole account is the number most likely to be misread: on 2026-08-10 a manual
 * FIG position masked an automated book down $1,289. Same figure, two entirely different stories.
 *
 * Attribution is by `StrategyId`, DISCOVERED from the data — not a hardcoded automated/manual binary.
 * MANUAL is a strategy like any other in this system (a trading style + a capital sleeve + a Nautilus
 * `StrategyId`); an earlier version of this file special-cased it, which both baked in a two-book world
 * that the architecture does not have and silently broke the moment a second automated strategy ran.
 * ETF_AUTO is already declared, and #68/#72 exist to give every strategy its own attribution.
 *
 * UNCLAIMED is kept separate from all of them, deliberately. Those are broker positions no strategy
 * owns — a human acting at the venue, or a restart that lost the originating order. Folding them into
 * MANUAL would charge a strategy for a decision it never made and corrupt the one record that is
 * supposed to say how that strategy performed.
 */
import type { TradeDTO } from "@/lib/api/types";
import { signedQty } from "@/lib/framework/signedQty";

/** One side of the split. */
export interface Book {
  realized: number;
  unrealized: number;
  total: number;
  /** Rows the broker holds none of, EXCLUDED from every figure above and counted here (#808 item 4). */
  phantom: number;
  /** Held positions with no mark — their unrealized is missing from `total`. */
  unknown: number;
  /** Held cycles/positions in this book. */
  held: number;
  /**
   * SIGNED Σ market value of the deployed positions (#699 a) — the `mv_now` end of
   * `net(W) = mv_now − mv_base − invested(W)`. `null` when any deployed position carries no mark:
   * unknown, never a smaller total.
   */
  marketValue: number | null;
}

/** The unclaimed-position shape this reads. Structural, so the tile's own interface satisfies it. */
export interface ExternalLike {
  realized_pnl?: string | null;
  unrealized_pl?: number | null;
  market_value?: number | null;
  /** The broker's quantity for the row's symbol (#807/#811): 0 = answered, holds none — a PHANTOM;
   *  null/undefined = not told yet. Only the phantom is excluded from money; "not told" still counts. */
  venue_qty?: number | null;
}

/** A row the broker holds NONE of. Its P&L is on stock the account does not own (#808 item 4). */
export function isPhantom(x: ExternalLike): boolean {
  return x.venue_qty === 0;
}

/** Parse a Money string ("1000.00 USD") → number. Currency dropped (single ccy v0). */
export function money(s: string | null | undefined): number {
  if (!s) return 0;
  const n = Number.parseFloat(s.split(" ")[0]);
  return Number.isFinite(n) ? n : 0;
}

/** One strategy's book, carrying the `StrategyId` it is attributed to. */
export interface StrategyBook {
  /** The wire `StrategyId` — `MOMENTUM-002`, `MANUAL-001`. The identity, not a display bucket. */
  strategyId: string;
  book: Book;
}

/** Every strategy present in the data, plus the positions no strategy owns. */
export interface Attribution {
  /** One entry per `StrategyId` found, ordered most-held then largest-|P&L|, then id for stability. */
  strategies: StrategyBook[];
  /** Broker positions no strategy claims. NOT folded into any strategy — see the file header. */
  unclaimed: Book;
}

const emptyBook = (): Book => ({ realized: 0, unrealized: 0, total: 0, unknown: 0, held: 0, phantom: 0, marketValue: 0 });

function add(b: Book, realized: number, deployed: boolean, unrealized: number | null | undefined,
             marketValue?: number | null): void {
  b.realized += realized;
  if (!deployed) return;
  b.held += 1;
  if (unrealized != null) b.unrealized += unrealized;
  // A held position with no mark contributes NO unrealized and is counted instead. Treating a missing
  // mark as 0 would read as "worth nothing" when it means "not known yet".
  else b.unknown += 1;
  // SIGNED, and one unmarked position makes the LANE's market value unknown (#699 a) — the window
  // net subtracts from it, and a smaller total would render as a confident loss.
  if (b.marketValue !== null) {
    b.marketValue = typeof marketValue === "number" && Number.isFinite(marketValue) ? b.marketValue + marketValue : null;
  }
}

/**
 * Attribute P&L to the `StrategyId` that owns each cycle.
 *
 * Works at CYCLE level, not instrument-group level. `groupByInstrument` deliberately merges cycles from
 * two strategies into one row for the net anchor, so a group's `realized` can span strategies and
 * cannot be attributed — grouping first would hand a whole instrument to whichever strategy happened to
 * appear first in the list.
 *
 * Strategies are discovered, never enumerated: whatever `StrategyId`s appear get a book. A new strategy
 * registered in the engine shows up here with no UI change, and no strategy is privileged over another.
 */
export function attribute(trades: TradeDTO[], external: ExternalLike[]): Attribution {
  const by = new Map<string, Book>();
  for (const t of trades) {
    const id = t.strategy_id || "UNKNOWN";
    let b = by.get(id);
    if (!b) by.set(id, (b = emptyBook()));
    add(b, money(t.realized_pnl), t.is_capital_deployed, (t as { unrealized_pl?: number | null }).unrealized_pl,
        (t as { market_value?: number | null }).market_value);
  }

  const unclaimed = emptyBook();
  for (const x of external) {
    // Every external POSITION is unclaimed by definition — the caller filters for `source === "POSITION"`.
    // Its `strategy_id` is a placeholder, not an owner, so it must not open a strategy book.
    // A PHANTOM CARRIES NO MONEY (#808 item 4). On 2026-09-09 four rows the broker held none of put
    // +$271.86 into this book, the header's standing unrealized and DEPLOYED. Counted, not summed.
    if (isPhantom(x)) {
      unclaimed.phantom += 1;
      continue;
    }
    add(unclaimed, money(x.realized_pnl), true, x.unrealized_pl, (x as { market_value?: number | null }).market_value);
  }

  const strategies: StrategyBook[] = [...by.entries()].map(([strategyId, book]) => {
    book.total = book.realized + book.unrealized;
    return { strategyId, book };
  });
  // Most-held first (what is live matters more than what is flat), then by size of P&L, then by id so
  // the order never jitters between frames on ties.
  strategies.sort(
    (a, b) =>
      b.book.held - a.book.held ||
      Math.abs(b.book.total) - Math.abs(a.book.total) ||
      a.strategyId.localeCompare(b.strategyId),
  );

  unclaimed.total = unclaimed.realized + unclaimed.unrealized;
  return { strategies, unclaimed };
}

/** What the held positions are worth at the mark, and how much of that is a guess. */
export interface Deployed {
  /** Σ |qty| × mark over held cycles + unclaimed market values. */
  value: number;
  /** Held positions with no mark — contributed NOTHING, so `value` is understated by their worth. */
  unmarked: number;
}

/**
 * Market value of everything held. This is the DEPLOYED side of the account: liquidation value is
 * roughly `cash + deployed`, and the gap between them is what is still at risk.
 *
 * An unmarked position contributes zero rather than a guess, and is COUNTED so the caller can say the
 * figure is partial. Silently treating "no mark yet" as "worth nothing" understates deployed capital,
 * which is the direction that makes a book look safer than it is.
 */
export function deployedValue(trades: TradeDTO[], external: ExternalLike[]): Deployed {
  let value = 0;
  let unmarked = 0;
  for (const t of trades) {
    if (!t.is_capital_deployed) continue;
    // `market_value` is marked SERVER-side and is first-class on the DTO — prefer it over recomputing
    // |qty| × last_px, which would be a second answer to a question the plane already answers (and one
    // that silently disagrees the moment the engine values anything differently). `last_px` is the
    // fallback only for a frame that predates the field.
    const mv = (t as { market_value?: number | null }).market_value;
    const px = (t as { last_px?: number | null }).last_px;
    if (mv != null) value += Math.abs(mv);
    else if (px != null) value += Math.abs(t.quantity) * px;
    else unmarked += 1;
  }
  for (const x of external) {
    if (isPhantom(x)) continue; // stock the account does not own is not deployed capital (#808 item 4)
    const mv = (x as { market_value?: number | null }).market_value;
    // |mv|, THE SAME CONVENTION AS THE MANAGED BRANCH ABOVE (#855). DEPLOYED asks how much capital is
    // COMMITTED, and a short commits capital rather than releasing it — `books.test.ts:264` pins that
    // for a cycle. This branch took the raw value, so one short contributed +1100 when a strategy
    // claimed it and −1100 when none did: claiming a row, which moves no stock, swung the figure by
    // twice its market value. (The broker's own long-only `long_market_value` reading is #876.)
    if (mv != null) value += Math.abs(mv);
    else unmarked += 1;
  }
  return { value, unmarked };
}

/** Gain already locked in by protective stops resting above (or below, for a short) entry. */
export interface Secured {
  /** Σ covered qty × (stop − entry), counting only stops that are actually in the money. */
  value: number;
  /** Held positions with a protective stop working. */
  covered: number;
  /** Held positions with NO protective order resting — nothing locked in, whatever the mark says. */
  naked: number;
  /**
   * Held positions protection can NEVER cover from here: no price, so `plan_protection` refuses to
   * size a stop, every tick, for as long as the venue withholds market data.
   *
   * SEPARATE FROM `naked` BECAUSE THEY ARE DIFFERENT FACTS. One is a backlog that drains; the other
   * does not, and reporting them as one number let 22 permanently-unprotectable positions on staging
   * read as an ordinary queue. A count that cannot go down must not share a cell with one that can.
   */
  unprotectable: number;
  /**
   * Covered holdings whose protection is an ENTRY FLOOR (#872) — a fixed stop at the lane's own entry
   * minus k x ATR, so it sits BELOW entry by construction and contributes nothing to `value`.
   *
   * NOT a fourth state: a floor is `covered`, fully. It is here so the caption can say WHICH KIND of
   * stop is resting, because "$0.00 secured / none above entry" on a lane protected exactly as
   * intended reads as an alarm, and an alarm that cries wolf is one an operator learns to scroll past.
   */
  floors: number;
}

/** Order types that protect a position. A resting LIMIT is a target, not a floor — it secures nothing. */
// PINNED AGAINST NAUTILUS'S REAL `OrderType`, member by member, in the backend's conformance test —
// and against THIS literal, because the same set living in two languages is the two-derivations-drift
// this file warns about elsewhere. Measured 2026-08-31: `TRAILING_STOP` and `STOP` are not Nautilus
// order types at all and matched nothing ever, while `TRAILING_STOP_LIMIT` — which is real and does
// protect — was missing, so a stop of that type rendered as an UNPROTECTED position.
const PROTECTIVE = new Set(["STOP_MARKET", "STOP_LIMIT", "TRAILING_STOP_MARKET", "TRAILING_STOP_LIMIT"]);

/** Statuses that mean the order is actually working at the venue. */
const LIVE_STATUS = new Set(["ACCEPTED", "TRIGGERED", "PARTIALLY_FILLED", "PENDING_UPDATE", "SUBMITTED"]);

interface WorkingOrderLike {
  side?: string;
  order_type?: string;
  quantity?: number;
  leaves_qty?: number;
  price?: number | null;
  trigger_price?: number | null;
  status?: string;
  /** The order's own tags, as the engine set them. Absent on an older frame — which is not "no". */
  tags?: string[];
}

/**
 * The tag the protection reconciler puts on an entry-floor stop (#872, `engine_node`).
 *
 * ONE DERIVATION, and it has to be the ENGINE's. Deriving "is this a floor" from `order_type` would
 * catch every bracket leg, every PEAK stop and every operator's own stop sell, all of which are
 * STOP_MARKETs on the reducing side — and it would drift from the mechanism that placed it the first
 * time either side changed. The order says what it is.
 */
const ENTRY_FLOOR_TAG = "mode:entry_floor";

/**
 * What is actually LOCKED IN — the gain a resting protective stop would realise if it triggered now.
 *
 * This is the one figure on the tile that does not move with the mark. Unrealized says what the book is
 * worth if nothing changes; secured says what survives if everything goes wrong at once. A position up
 * 8% with no stop under it has secured NOTHING, and the difference between those two numbers is the
 * part that can still evaporate.
 *
 * Counts a stop only when it is genuinely in the money — above entry for a long, below for a short — and
 * only for the quantity it actually covers (`leaves_qty` capped at the position). A stop under entry is
 * loss limitation, not a secured gain, and adding it as a negative would net against real locked-in
 * profit elsewhere and understate what is safe.
 *
 * Targets are excluded on purpose: a resting LIMIT above the market secures nothing, because nothing
 * stops the price falling through entry before it is reached.
 */
/** The resting orders on a cycle that actually protect it — right type, live, and reducing. */
export function protectiveOrdersOn(t: TradeDTO): WorkingOrderLike[] {
  const long = t.side !== "SHORT";
  return ((t as { working_orders?: WorkingOrderLike[] }).working_orders ?? []).filter(
    (w) =>
      PROTECTIVE.has((w.order_type ?? "").toUpperCase()) &&
      (w.status == null || LIVE_STATUS.has(w.status.toUpperCase())) &&
      // Reduces the position: a sell protects a long, a buy protects a short.
      (long ? w.side === "SELL" : w.side === "BUY"),
  );
}

/**
 * Is this cycle protected? ONE predicate, used by the SECURED tally and by any filter that offers
 * "unprotected" as a view.
 *
 * Extracted rather than reimplemented at the call site (#251). This logic is not obvious — it overrides
 * the cache with broker truth in one direction and with cache truth in the other, for reasons #285
 * paid for — and a second copy would drift the first time either side changed. Two derivations of one
 * fact disagree; a filter that says "unprotected" while SECURED says covered is worse than no filter,
 * because it is a safety claim and they cannot both be right.
 */
export function isProtected(t: TradeDTO): boolean {
  const orders = protectiveOrdersOn(t);
  const brokerSaysProtected = (t as { broker_protected?: boolean | null }).broker_protected;
  if (orders.length === 0) return brokerSaysProtected === true;
  // The mirror case: the cache believes a stop is working and the broker says nothing is resting.
  // Counting that as covered would hide a genuinely naked position behind a stale order.
  return brokerSaysProtected !== false;
}

/**
 * Protected, not yet, or never — three states, because two of them were being reported as one.
 *
 * MEASURED 2026-08-31: paper showed "9 of 38 unprotected" and staging "Unprotected 22", rendered
 * identically. Paper's are positions protection has not covered YET. Staging's are positions it CAN
 * NEVER cover: 11 of its 22 have no price, and `plan_protection` refuses to size a stop it cannot
 * price — every tick, forever, while the venue refuses market data. One is a backlog that drains;
 * the other never does, and collapsing them let the second read as the first.
 *
 * `unpriceable` is the engine's own `unpriced_positions` from /health, not a second derivation here:
 * the engine knows what it could price because it tried, and computing it again from the frame would
 * be the two-derivations-drift shape this file already warns about for `isProtected`.
 *
 * A RESTING STOP WINS OVER UNPRICEABILITY. A stop placed while a price existed keeps protecting
 * after the feed goes; calling that unprotectable is a false alarm on a covered position, which is
 * the direction that gets a safety badge ignored.
 *
 * `undefined` is NOT an empty list. It means the engine did not tell us — an older frame, a degraded
 * read — and must not silently assert that everything is priceable.
 */
export type Protectability = "protected" | "unprotected" | "unprotectable";

export function protectability(t: TradeDTO, unpriceable?: string[]): Protectability {
  if (isProtected(t)) return "protected";
  const iid = String((t as { instrument_id?: unknown }).instrument_id ?? "");
  if (unpriceable && unpriceable.includes(iid)) return "unprotectable";
  return "unprotected";
}

/**
 * The resting ENTRY FLOORS on a cycle — protective stops the engine placed at `entry − k × ATR` (#872).
 *
 * A subset of `protectiveOrdersOn`, never a parallel filter: a floor that this said was resting while
 * the coverage predicate said it was not would be a safety claim that is wrong on one of the two
 * screens showing it, which is the drift `isProtected` already carries a paragraph about.
 */
export function floorStopsOn(t: TradeDTO): WorkingOrderLike[] {
  return protectiveOrdersOn(t).filter((w) => (w.tags ?? []).includes(ENTRY_FLOOR_TAG));
}

/**
 * "floor 451.69" — the kind AND where it sits, for the book row.
 *
 * `undefined` when nothing is a floor, so an ordinary trailed row stays quiet rather than carrying a
 * label saying it is not something.
 *
 * EVERY RUNG, not the first. A scale-in re-weights the lane's entry and the added shares get a SECOND
 * floor at the new one — a ladder, by design — so rendering one of two would be a number that is true
 * about half the position.
 */
export function floorLabel(t: TradeDTO): string | undefined {
  const triggers = floorStopsOn(t)
    .map((w) => w.trigger_price ?? w.price)
    .filter((p): p is number => p != null);
  return triggers.length === 0 ? undefined : `floor ${triggers.map((p) => p.toFixed(2)).join(" · ")}`;
}

export function securedValue(trades: TradeDTO[], unpriceable?: string[]): Secured {
  let value = 0;
  let covered = 0;
  let naked = 0;
  let unprotectable = 0;
  let floors = 0;

  for (const t of trades) {
    if (!t.is_capital_deployed) continue;
    const entry = t.avg_px_open;
    const long = t.side !== "SHORT";
    const orders = protectiveOrdersOn(t);
    // BROKER TRUTH WINS (#285). The engine holds orders as REJECTED that Alpaca reports OPEN — a submit
    // whose HTTP call failed after the venue accepted it — and Nautilus refuses REJECTED -> ACCEPTED, so
    // reconciliation cannot repair them. They vanish from `working_orders` and the tile reported 8 of 8
    // positions unprotected while 8 GTC stops rested at the broker.
    //
    // A false "unprotected" is a safety claim that is wrong, and an alarm that cries wolf is one an
    // operator learns to scroll past — so the broker's answer overrides the cache's silence.
    //
    // `undefined`/null means the broker has NOT been asked, which is not the same as "nothing resting":
    // there the cache is all we have and its answer stands.
    // THE SAME PREDICATE the "unprotected" filter uses — see `isProtected`. Deliberately not inlined
    // again here: a tally and a filter that disagree about what "protected" means is a safety claim
    // that is wrong on one of the two screens showing it.
    // THROUGH `protectability`, not a second copy of the rule. This tally and that function
    // disagreeing about what "unprotectable" means is the two-derivations-drift shape this file
    // already warns about for `isProtected` — and the function shipped correct and CALLED BY
    // NOTHING, which is why the split never reached the screen it was written for.
    const state = protectability(t, unpriceable);
    if (state === "unprotectable") {
      unprotectable += 1;
      continue;
    }
    if (state === "unprotected") {
      naked += 1;
      continue;
    }
    covered += 1;
    // WHAT KIND of protection this is. `value` below counts only `gain > 0`, and an entry floor sits
    // BELOW entry by construction — so a lane protected exactly as intended reads "$0.00 · none above
    // entry", which is two true lines that together say "nothing is protecting this". The tally stays
    // as it is (a floor genuinely secures no GAIN); this is what lets the caption say which it is.
    if (floorStopsOn(t).length > 0) floors += 1;
    if (orders.length === 0) continue; // broker-confirmed but nothing local to price the stop from
    if (entry == null) continue;
    const size = Math.abs(t.quantity);
    for (const w of orders) {
      const stop = w.trigger_price ?? w.price;
      if (stop == null) continue;
      const qty = Math.min(Math.abs(w.leaves_qty ?? w.quantity ?? 0), size);
      const gain = long ? stop - entry : entry - stop;
      if (gain > 0 && qty > 0) value += gain * qty;
    }
  }
  return { value, covered, naked, unprotectable, floors };
}

/** Today's move on what is held, and how much of the book it could not be computed for. */
export interface DayMove {
  /** Σ qty × (mark − prior close) over held cycles with both a mark and a prior close. */
  value: number;
  /** Held positions missing a mark or a prior close — excluded, so `value` is partial. */
  missing: number;
  /** Held positions the figure DOES cover. Zero means the value is meaningless, not flat. */
  covered: number;
}

/**
 * Today's move on the positions currently held: Σ qty × (mark − prior close).
 *
 * This is NOT account day P&L, and the difference matters. It covers only what is still open — anything
 * closed today is already realized and invisible here, and cash movements are not in it at all. It is
 * "what my holdings did today", which is the question a position board is actually asked. Account-level
 * day P&L needs the broker's own equity curve (#233) and an engine change.
 *
 * A position opened TODAY is measured from the prior close, not from its entry, so it reports the day's
 * move in the name rather than the trade's P&L — which is what "day change" means everywhere else on
 * the screen (the watchlist computes chg% the same way, off the same `prev_close`).
 *
 * A missing prior close excludes the position and is counted rather than treated as zero, because a
 * silent zero would read as "flat today" for a name that may have moved sharply.
 */
/** The ET calendar date for an epoch-ms instant — the session convention used everywhere else. */
function etDate(ms: number): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(ms));
}

/**
 * Was this cycle opened in the CURRENT ET session? `opened_ts` is epoch NANOseconds.
 *
 * KNOWN LIMIT (codex review, High): `opened_ts` is the CYCLE's first-open anchor and is stable across
 * legs, while `avg_px_open` tracks the current leg. A cycle first opened yesterday, taken flat, then
 * re-entered today therefore still measures from the prior close and keeps the inflated figure. The
 * error direction is the OLD behaviour rather than a new one, so this is a narrowing, not a full fix.
 * Closing it needs the current leg's open timestamp — or better, the broker's own
 * `unrealized_intraday_pl`, which Alpaca already computes and our DTO does not yet carry. Tracked
 * separately; that is an engine change and the market was open when this shipped.
 */
export function openedToday(openedTsNs: number | null | undefined, nowMs: number): boolean {
  if (openedTsNs == null) return false;
  return etDate(openedTsNs / 1e6) === etDate(nowMs);
}

export function dayMove(
  trades: TradeDTO[],
  priorCloses: Map<string, number>,
  nowMs: number = Date.now(),
): DayMove {
  let value = 0;
  let missing = 0;
  let covered = 0;
  for (const t of trades) {
    if (!t.is_capital_deployed) continue;
    const mark = (t as { last_px?: number | null }).last_px;
    const prior = priorCloses.get(t.instrument_id);
    // A position opened TODAY is measured from ITS ENTRY, not from the prior close.
    //
    // This used to measure everything from the prior close, on the reasoning that "day change" should
    // mean the same thing here as in the watchlist. It does not: the watchlist reports a QUOTE moving,
    // this tile reports P&L, and for a position bought part-way through a move the two diverge wildly.
    // NBIS on 2026-08-12 closed 193.23 -> 250.23, up 29%. The operator bought at 246.91 in the afternoon and
    // made about $46; the tile credited him with $798, and the Home header read `day $995.66` against a
    // real figure of $278.66. Measuring from entry is also what the broker's own
    // `unrealized_intraday_pl` does, so the two now agree instead of contradicting each other.
    const opened = (t as { opened_ts?: number | null }).opened_ts;
    const entry = (t as { avg_px_open?: number | null }).avg_px_open;
    const fromEntry = openedToday(opened, nowMs) && entry != null;
    const basis = fromEntry ? entry : prior;
    if (mark == null || basis == null) {
      missing += 1;
      continue;
    }
    covered += 1;
    // Signed quantity: a short gains when the price falls.
    const qty = signedQty(t.side, t.quantity);
    value += qty * (mark - basis);
  }
  return { value, missing, covered };
}

/** Per-strategy day move, keyed by `StrategyId`, plus one for the unclaimed book. */
export function dayMoveByStrategy(
  trades: TradeDTO[],
  priorCloses: Map<string, number>,
  nowMs: number = Date.now(),
): Map<string, DayMove> {
  const out = new Map<string, DayMove>();
  const byId = new Map<string, TradeDTO[]>();
  for (const t of trades) {
    const id = t.strategy_id || "UNKNOWN";
    const list = byId.get(id);
    if (list) list.push(t);
    else byId.set(id, [t]);
  }
  for (const [id, list] of byId) out.set(id, dayMove(list, priorCloses, nowMs));
  return out;
}

/** A book worth drawing: it holds something, or it closed something. */
export function speaks(b: Book): boolean {
  return b.held > 0 || b.realized !== 0;
}

/**
 * The books to draw, in order, unclaimed last. Silent books are dropped — a cell reading "$0.00 · 0
 * held" is noise the account total already covers.
 *
 * Returns nothing when only ONE book would show: with a single strategy the breakdown just restates
 * the net above it.
 */
export function visibleBooks(
  a: Attribution,
  /**
   * The selected period's swept realized per strategy (`realized_periods[p].by_strategy`), when the
   * caller has it. OPTIONAL — every pre-existing caller passes nothing and is unaffected.
   *
   * WHY IT IS NEEDED (#523). The rows above come from TRADE CYCLES, and a CLOSED cycle is emitted
   * once and dropped from the projection (BookTile:206). So a strategy holding nothing has no book,
   * cannot `speak()`, and vanishes — taking its realized with it. On 2026-08-24 the panel listed
   * MOMENTUM-002, BCTROT-004 and TECHIVOL-005 while the engine's own sweep put MANUAL-001 at
   * +1522.07 over 1M, the largest realized contributor in the book.
   *
   * That is two derivations of "which strategies have P&L" — live cycles and swept fills — and the
   * panel was rendering their INTERSECTION. The union is the honest set.
   */
  periodRealizedByStrategy?: Record<string, number> | null,
  /**
   * The window's swept UNCLAIMED figure — `rowPeriodRealized(frame, period, "Unclaimed")`, which is
   * EXTERNAL plus the sweep's own residual. OPTIONAL; callers passing nothing behave as before.
   *
   * WHY IT IS NEEDED. The EXTERNAL filter below drops that money on the stated grounds that it
   * "already has its own Unclaimed row" — but the row beneath is only emitted when the LIVE unclaimed
   * book `speaks()`, i.e. when a broker position is currently held with no strategy. With none, the
   * filter drops EXTERNAL and nothing picks it up: rendered on the real tile with `external_activity`
   * empty, five cells summed to 4,371.87 against an engine total of 3,278.71 and the missing
   * -1,093.16 appeared NOWHERE. The book read ~$1,093 better than it is, on the default screen.
   *
   * That is #596 one row over — and it is a WRONG number, not a missing one, which is the worse half.
   */
  sweptUnclaimed?: number | null,
): Array<{ label: string; book: Book }> {
  const withCycles = new Set(a.strategies.map((s) => s.strategyId));
  const flat = Object.entries(periodRealizedByStrategy ?? {})
    // EXTERNAL is not a strategy — it already has its own "Unclaimed" row and must not grow a second.
    // A zero contribution earns no row: the panel is read at a glance, and one line per strategy that
    // has ever existed buries the ones that moved.
    .filter(([id, v]) => !withCycles.has(id) && id !== "EXTERNAL" && v !== 0)
    // An EMPTY book, deliberately. The row exists so the swept realized has somewhere to render;
    // held and unrealized are genuinely zero, and fabricating them would make the panel's own total
    // disagree with itself.
    .map(([id]) => ({ label: id, book: emptyBook() }));
  const rows = [
    ...a.strategies.filter((s) => speaks(s.book)).map((s) => ({ label: s.strategyId, book: s.book })),
    ...flat,
    // SYMMETRIC WITH `flat` ABOVE. A live unclaimed book gets its real book; a silent one still gets
    // an EMPTY row when the sweep has money to put in it, exactly as a flat strategy does. Without
    // this the EXTERNAL filter is a leak rather than a de-duplication.
    ...(speaks(a.unclaimed)
      ? [{ label: "Unclaimed", book: a.unclaimed }]
      : (sweptUnclaimed ?? 0) !== 0
        ? [{ label: "Unclaimed", book: emptyBook() }]
        : []),
  ];
  return rows.length > 1 ? rows : [];
}

/** The engine's session-realized field, as it rides the trades frame. */
export interface SessionRealizedLike {
  total?: number | null;
  partial_open?: number | null;
  is_partial?: boolean | null;
}

/**
 * Realized P&L for the session — the ENGINE's figure when it offers one, else the live-cycle sum (#233).
 *
 * Summing the live cycles is structurally wrong and was the bug: a CLOSED cycle is emitted once and
 * dropped from the projection, so the sum only ever covers what is still open. The tile read $0.00 on a
 * day four MANUAL positions had been closed for +$483, and it also hid the strategy tiles, because
 * `speaks()` asks `held > 0 || realized !== 0` and a fully-exited strategy answered no to both.
 *
 * The fallback is kept deliberately rather than defaulting to zero: a frame that predates the field, or
 * a node with no engine, should show the old imperfect number instead of a confident $0.00. Zero is a
 * claim; absent is not.
 */
export function sessionRealized(
  frame: { realized_session?: SessionRealizedLike | null } | undefined,
  fallback: number,
): number {
  const total = frame?.realized_session?.total;
  return typeof total === "number" && Number.isFinite(total) ? total : fallback;
}

/** Realized P&L for a chosen PERIOD, from the broker sweep (#322), falling back to the session figure.
 *
 * `realized_session` answers only for TODAY, and can only ever answer for today: it reads
 * `cache.positions_closed()`, which holds positions closed during the current engine process. A restart
 * zeroes it. That is why REALIZED read $1,061 on a week the equity curve had moved ~$2,841 — the rest
 * was realized on positions the cache had already forgotten.
 *
 * Order of preference, and each step is a different kind of "no":
 *   1. the swept figure for THIS period — the answer
 *   2. `1D` falls back to `realized_session` — same window, native source, arrives sooner after a boot
 *   3. `null` for any other period — NOT ZERO. A week whose sweep has not landed is unknown, and
 *      rendering $0.00 would assert that nothing closed all week.
 */
export function periodRealized(
  frame:
    | {
        realized_session?: SessionRealizedLike | null;
        realized_periods?: Record<string, { total?: number | null; error?: string | null } | null> | null;
      }
    | undefined,
  period: string,
  sessionFallback: number,
): number | null {
  const w = frame?.realized_periods?.[period];
  // A window that could not be computed carries `error` and no `total` (#846). It must render as
  // unknown — never fall through to the session figure, which is the same refused fold.
  if (w?.error) return null;
  const legs = w?.total;
  if (typeof legs === "number" && Number.isFinite(legs)) return legs;
  if (period === "1D") return sessionRealized(frame, sessionFallback);
  return null;
}

/** Is a period's realized figure INCOMPLETE, and by how much? (#322, #846)
 *
 * `unmatched` is the frame's word for "the total UNDERSTATES this window", and it now has two sources
 * with two causes. From the legs (`realized_periods`, every venue since #846): partial exits on
 * positions still OPEN — native realizes their P&L but cannot timestamp it, so it is counted, never
 * folded in. From the broker sweep (`realized_periods_swept`, Alpaca): sells whose opening buy falls
 * OUTSIDE the window, contributing proceeds with no basis.
 *
 * The sweep's case is why a shorter window could exceed a longer one. Live on 2026-08-17: 1W read
 * $2,195.32 with 19 unmatched, All $107.70 with none. Both were correct. Without surfacing it the
 * panel showed a week that made more than the account's entire history and looked broken.
 */
export function periodRealizedPartial(
  frame: { realized_periods?: Record<string, { unmatched?: number | null } | null> | null } | undefined,
  period: string,
): { partial: boolean; unmatched: number } {
  const n = frame?.realized_periods?.[period]?.unmatched;
  const unmatched = typeof n === "number" && Number.isFinite(n) ? n : 0;
  return { partial: unmatched > 0, unmatched };
}


/** How far back a window can see (#846): the oldest closed leg this engine holds, epoch ns.
 *
 * `all` is NOT account inception — on ibkr-paper it is 2026-09-03, the day that cache was born. A figure
 * that cannot say its horizon reads as a lifetime number, which it is not. Null when the frame carries
 * no horizon (an older engine, or no legs at all — "unknown", never "since forever").
 */
export function periodHorizonTs(
  frame: { realized_periods?: Record<string, { horizon_ts?: number | null } | null> | null } | undefined,
  period: string,
): number | null {
  const h = frame?.realized_periods?.[period]?.horizon_ts;
  return typeof h === "number" && Number.isFinite(h) ? h : null;
}

/** The BROKER's own realized for this window, where a fill sweep exists (Alpaca: `realized_periods_swept`).
 *
 * `net`, not `total`: the sweep's `total` is FILLS ONLY by pinned design (`test_total_KEEPS_its_meaning`),
 * and `net` is what carries the fees and withholding the legs cannot see — the $999.09 that justifies
 * keeping the sweep at all. It reaches account inception, so it WILL differ from `periodRealized`. Shown
 * beside the legs' figure, never averaged into it — the two disagreeing is the detector that found #846.
 * Null on a venue with no ledger (IBKR) or before the first sweep.
 */
export function periodBrokerRealized(
  frame: { realized_periods_swept?: Record<string, { net?: number | null } | null> | null } | undefined,
  period: string,
): number | null {
  const n = frame?.realized_periods_swept?.[period]?.net;
  return typeof n === "number" && Number.isFinite(n) ? n : null;
}

/**
 * Realized in this window for ONE strategy — the OPENING strategy's, per #292 (#345 item 3).
 *
 * `Book.realized` cannot answer this. It sums `realized_pnl` off the live cycles, which is the SESSION
 * figure: Nautilus's cache holds positions closed during this process's life and reconciliation restores
 * OPEN positions on startup, not closed ones. So after any restart every strategy cell reads
 * `real $0.00`. Proof off the 2026-08-18 screen — both cells printed $0.00 while the book had realized
 * $2,650.21 over 1W, and the two cells summed to exactly UNREALIZED:
 *
 *     MOMENTUM-002 1,245.59 + MANUAL-001 (-68.80) = 1,176.79 = UNREALIZED, exactly
 *
 * The engine now sweeps the broker's fills and attributes each realization to the lot that OPENED it,
 * so this reads a swept figure rather than a session one and survives a restart.
 *
 * `null` means the sweep has not produced a figure for this window — NOT zero. A strategy with no
 * realizations in the window is a real 0 and is returned as one; the two must not render alike.
 */
export function strategyPeriodRealized(
  frame:
    | { realized_periods?: Record<string, { by_strategy?: Record<string, number> | null } | null> | null }
    | undefined,
  period: string,
  strategyId: string,
): number | null {
  const by = frame?.realized_periods?.[period]?.by_strategy;
  if (!by || typeof by !== "object") return null;
  const v = by[strategyId];
  if (typeof v === "number" && Number.isFinite(v)) return v;
  // The window was swept and this strategy is simply not in it — that is a genuine zero, not an unknown.
  return 0;
}

/**
 * The whole swept per-strategy map for a window, for callers that need to know WHICH strategies the
 * sweep saw — not just what one of them realized.
 *
 * `visibleBooks` needs exactly this: the row list is built from live trade cycles, and a strategy
 * that is flat has none, so without the sweep's own key set a flat strategy cannot be listed at all
 * (#523). Returns `null` when the window has not been swept, which is not the same as an empty map.
 */
export function periodRealizedByStrategy(
  frame:
    | { realized_periods?: Record<string, { by_strategy?: Record<string, number> | null } | null> | null }
    | undefined,
  period: string,
): Record<string, number> | null {
  const by = frame?.realized_periods?.[period]?.by_strategy;
  return by && typeof by === "object" ? by : null;
}

/**
 * Realized in this window whose OPENING lot has no known strategy (#345 item 3).
 *
 * Not a rounding bucket. Measured 2026-08-21: fills join to a cached order 100% of the time from
 * 2026-08-17 and ~0% before it, because the Nautilus cache does not reach further back. So on `all` the
 * unattributed share is most of the figure, and a per-strategy breakdown that omitted it would look
 * complete while accounting for a fraction of the money.
 *
 * The invariant the engine publishes against: `Σ(by_strategy) + unclaimed === total`.
 */
/**
 * Realized for the UNCLAIMED row — the engine's residual PLUS everything bucketed under EXTERNAL.
 *
 * THE ROWS DID NOT SUM TO THE TOTAL, and the operator said so twice before it was fixed. `visibleBooks`
 * drops EXTERNAL because it is not a strategy and must not grow a second row; the Unclaimed row then
 * looked itself up by its own LABEL, and `by_strategy["Unclaimed"]` does not exist — so
 * `strategyPeriodRealized` returned its "genuine zero" and the money vanished from the panel.
 *
 * Measured on an Alpaca paper instance 2026-08-27, the ALL window:
 *
 *     MOMENTUM-002   3246.87
 *     MANUAL-001     1522.07
 *     EXTERNAL      -1093.30   <- rendered nowhere
 *     TECHIVOL-005    -77.59
 *     BCTROT-004      -67.53
 *     unclaimed         0.14
 *     total          3530.66   <- INCLUDES the -1093.30 the rows left out
 *
 * So the cells summed to 4623.82 against a total of 3530.66 — a 1093.30 hole, on a panel whose whole
 * job is to add up. The total was right the entire time; only the breakdown lied.
 *
 * BOTH TERMS, not either: `unclaimed` is the sweep's own residual and EXTERNAL is activity attributed
 * to no strategy. They are different quantities and the row owes the reader both.
 */
export function unclaimedPeriodRealized(
  frame:
    | {
        realized_periods?: Record<
          string,
          { by_strategy?: Record<string, number> | null; unclaimed?: number | null } | null
        > | null;
      }
    | undefined,
  period: string,
): number | null {
  const row = frame?.realized_periods?.[period];
  if (!row) return null;
  const ext = row.by_strategy?.EXTERNAL;
  const residual = row.unclaimed;
  const a = typeof ext === "number" && Number.isFinite(ext) ? ext : null;
  const b = typeof residual === "number" && Number.isFinite(residual) ? residual : null;
  // Neither term present means the window was not swept — unknown, not zero.
  if (a === null && b === null) return null;
  return (a ?? 0) + (b ?? 0);
}


/**
 * Swept realized for ONE ROW of the per-strategy panel, addressed the way the panel addresses it — by
 * its LABEL. The single derivation both tiles call.
 *
 * IT IS NOT A PLAIN LOOKUP, which is exactly why it lives here. The "Unclaimed" row is bucketed under
 * `EXTERNAL` plus the sweep's own residual, not under its own label, so asking `by_strategy["Unclaimed"]`
 * returns `strategyPeriodRealized`'s "genuine zero" branch and the money silently disappears from a panel
 * whose job is to add up. That is #596: -1,093.30 vanished, the rows did not sum to the total, and the operator
 * said so twice before it was fixed.
 *
 * BookTile had this ternary inline and ManagedPortfolioTile had nothing at all. Two derivations of one
 * fact drift — this repo's most expensive defect class — and the second tile catching up was the moment
 * to make it one. A copy in each tile would mean the next `Unclaimed`-shaped correction lands in one
 * panel and not the other, which is precisely how #596 read on screen.
 *
 * `null` means the window has not been swept: unknown, NOT zero. The caller must render it as such.
 */
export function rowPeriodRealized(
  frame:
    | {
        realized_periods?: Record<
          string,
          { by_strategy?: Record<string, number> | null; unclaimed?: number | null } | null
        > | null;
      }
    | undefined,
  period: string,
  label: string,
): number | null {
  return label === "Unclaimed"
    ? unclaimedPeriodRealized(frame, period)
    : strategyPeriodRealized(frame, period, label);
}

export function periodUnclaimedRealized(
  frame: { realized_periods?: Record<string, { unclaimed?: number | null } | null> | null } | undefined,
  period: string,
): number | null {
  const v = frame?.realized_periods?.[period]?.unclaimed;
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}


/**
 * What a strategy cell can honestly say about today's move (#345 item 6).
 *
 * `BookCell` gated on `day.covered > 0` and rendered NOTHING otherwise, so three different situations
 * looked identical — a strategy holding nothing, a strategy whose prior closes never arrived, and a
 * strategy whose day figure is simply absent. Only the first of those is benign.
 *
 * Prior closes ride the shared `today_ranges` plane over the websocket, and the engine TOMBSTONES a
 * symbol whose snapshot went stale (`high: null`) precisely so a day change is not computed against the
 * wrong session. When that plane is empty or fully tombstoned, `covered` is 0 for every strategy at
 * once and the line disappears from the whole panel — a dead feed rendering as absence.
 *
 * That is #298's failure, which this repo has paid for twice: the engine had to learn to report WHICH
 * KIND of empty it was, and on 2026-08-14 an empty tile stood in for eight held positions. A number
 * that cannot be computed must say so; it must not quietly not be there.
 */
export type DayReading =
  | { kind: "value"; value: number; missing: number }
  | { kind: "unavailable"; missing: number }
  | { kind: "nothing" };

export function readDayMove(day: DayMove | undefined | null): DayReading {
  if (!day) return { kind: "nothing" };
  if (day.covered > 0) return { kind: "value", value: day.value, missing: day.missing };
  // Nothing covered, but positions ARE held and were excluded — the prior closes did not arrive. This
  // is the case that must not render as silence.
  if (day.missing > 0) return { kind: "unavailable", missing: day.missing };
  // Nothing covered and nothing excluded: the strategy holds nothing. The cell already says "0 held",
  // so a day line would be noise rather than information.
  return { kind: "nothing" };
}

/**
 * WHAT THE LARGEST NUMBER IN A STRATEGY CELL MEANS (#699).
 *
 * Operator, from the live 1M screen: the cell led with a standing LEVEL — session realized plus the whole
 * standing unrealized — while sitting under a window selector, beside a hero reading DELTA NET · 1M.
 * Three time bases in one cell, and the largest type on the one the selector does not govern:
 *
 *     TECHIVOL-005
 *     $790.82                              <- a level, unchanged by the selector
 *     day -$393.54                         <- a 1D fact
 *     11 held · standing · real -$57.47    <- the only windowed figure, in the smallest type
 *
 * THE IDEAL IS NOT BUILDABLE TODAY. The ticket asks for `realized(W) + Δunrealized(W)` per lane — the
 * identity the account headline uses. The broker publishes ACCOUNT-level equity curves only, so there
 * is no per-lane mark at a window's start, and a position opened inside the window has none at all.
 * Persisting a per-lane unrealized snapshot per day is the real fix (#699 option a) and covers windows
 * only from the day it ships.
 *
 * SO THE HEADLINE IS REALIZED FOR THE WINDOW, AND SAYS SO. Honest, already computed, and it is the
 * figure the panel's rows sum to. The cost is real and is stated rather than hidden: this composes
 * differently from DELTA NET above it, which is realized + Δunrealized — so the cell NAMES what it is
 * showing instead of leaving the reader to assume the two match.
 *
 * REFUSED, still: `realized(W) + CURRENT standing unrealized`. That reports months of accrued mark as
 * this window's performance, which is #336 exactly.
 *
 * 1D KEEPS ITS DAY MOVE, because 1D is the one window that IS answerable — `dayMoveByStrategy` is
 * qty x (mark - prior close) on what is held, a true delta over that window, on the same basis rule
 * Home uses. Replacing it with realized would throw away the only honest per-window delta there is.
 *
 * ONE HELPER, TWO TILES. BookTile and ManagedPortfolioTile both render this cell, and #523 cost three
 * defects precisely because they carried two copies of one rule. What the headline MEANS is decided
 * here, once.
 */
//: THE ONE DAY READING, exposed rather than consumed. Both cells render their own day line from it:
//: BookTile used to call `readDayMove` a SECOND time and ManagedPortfolioTile could not derive it at
//: all, so the two tiles disagreed about whether today was even sayable. Returning it is what makes
//: "one rule, two tiles" true of the day plane and not only of the headline.
type WithDayReading = { day: DayReading };

/**
 * DISCRIMINATED, so the compiler holds the invariant instead of a comment.
 *
 * A flat `{ kind; value: number | null }` shape let a WINDOW branch return null and still typecheck —
 * verified: mutating the window branch to `value: null` passed tsc clean, and only the runtime tests
 * caught it. That moved "null ONLY for unswept" out of the type and into a sentence beside a value,
 * which is the exact thing this file's own history is about.
 *
 * `null` is "this window has not been swept". Not zero — rendering the unknown as a number is the
 * claim this repo keeps paying for, and it is worse in the headline than it ever was in the sub-line.
 */
export type CellHeadline = (
  | { kind: "day" | "window"; value: number }
  /** The lane's NET OF FLOWS over the window (#699 a): `mv_now − mv_base − invested(W)`. */
  | { kind: "net"; value: number; partial: string | null }
  | { kind: "unswept"; value: null }
) &
  WithDayReading & {
    /**
     * The window's change in this lane's unrealized, CARRIED BESIDE the headline and never summed
     * into it. `null` when unknown. See the comment in `cellHeadline` for why adding it to
     * `realized(W)` is wrong: the two use different basis rules and a partial close of
     * heterogeneous lots makes the sum wrong by `closed_qty x (avg - fifo_lot_basis)`.
     */
    unrealizedDelta?: number | null;
  };

export function cellHeadline(
  period: string,
  day: DayMove | undefined | null,
  periodRealized: number | null,
  /**
   * The window's change in this lane's unrealized, from `GET /pnl/unrealized-base` (#699/#734).
   *
   * `null` or absent means UNKNOWN — the base day was never captured, the window has no base, or the
   * read failed. It must NOT become zero: `realized(W) + 0` renders as a complete NET and is a
   * smaller, more confident lie than labelling the number for what it is. The cell falls back to
   * window realized, which is what it showed before this existed.
   */
  unrealizedDelta?: number | null,
  /**
   * The lane's window NET OF FLOWS from `windowNet` (#699 a) — the identity the ticket asked for,
   * built on terms that HAVE one basis: `mv_now − mv_base − invested(W)`. When known it is the
   * headline for every window but 1D; when unknown the cell falls back to window realized, as
   * before. Absent means the caller predates it.
   */
  net?: { value: number | null; partial: string | null } | null,
): CellHeadline {
  const reading = readDayMove(day);
  // NON-FINITE IS UNKNOWN. A NaN reaching a cell renders as "NaN" and a subtraction against it
  // poisons anything downstream, so it is refused here rather than propagated.
  const normalisedDelta =
    typeof unrealizedDelta === "number" && Number.isFinite(unrealizedDelta) ? unrealizedDelta : null;
  // 1D is the one window with a TRUE per-lane delta: qty x (mark - prior close) on what is held, the
  // same basis rule Home uses. Every other window would need a per-lane mark at the window's start,
  // which the broker does not publish (#699).
  if (period === "1D" && reading.kind === "value") {
    return { kind: "day", value: reading.value, day: reading };
  }
  // THE NET OF FLOWS, when it is known (#699 a). `ΔMV − invested` has no basis rule in it, so the
  // objection below to summing realized and Δunrealized does not apply: the same counter-example
  // nets to zero. FIFO realized and the mark delta still ride along for the small print.
  if (net && typeof net.value === "number" && Number.isFinite(net.value)) {
    return { kind: "net", value: net.value, partial: net.partial ?? null, day: reading, unrealizedDelta: normalisedDelta };
  }
  // AN UNSWEPT WINDOW STAYS UNSWEPT whatever the delta says. A mark change without its realized half
  // is not a net, and reporting it under a NET label answers a question nobody asked.
  if (periodRealized === null) return { kind: "unswept", value: null, day: reading };
  // THE TWO TERMS ARE NOT ADDABLE, AND I SHIPPED A COMMIT THAT ADDED THEM. Review constructed the
  // counter-example and I reproduced it: `realized(W)` is FIFO BY LOT (`realized_broker._match`)
  // while BOTH ends of Δunrealized use `avg_px_open`, which under NETTING does not move on a partial
  // close. Buy 50@10 and 50@20 (avg 15), window-start mark 20 so the base is 500, then sell 50 @20
  // inside the window with the mark unchanged:
  //
  //     FIFO realized      50 x (20 - 10)  = 500
  //     unrealized now     50 x (20 - 15)  = 250      delta = -250
  //     NET as summed                       = 250
  //     economic truth                      =   0     (the mark never moved)
  //
  // The error is `closed_qty x (avg - fifo_lot_basis)`. Full closes wash out because the FIFO total
  // equals the average total; PARTIAL closes of heterogeneous lots do not, and momentum scales out
  // routinely — so this would be wrong on the live book, not in theory.
  //
  // So the delta is CARRIED, not summed. The headline stays window realized, labelled as realized;
  // the cell renders the mark change as its own figure beside it. Two honest numbers beat one
  // confident wrong one, and a NET needs both terms on ONE basis rule — filed rather than faked.
  return { kind: "window", value: periodRealized, day: reading, unrealizedDelta: normalisedDelta };
}
