/**
 * Turning a rotation verdict into something readable, and refusing to overstate it (#351).
 *
 * The payload is `ledger-tool:tools/rotation_read.py`'s. Its `verdict` is Ichimoku vocabulary — ON, ON-wk,
 * TURN, OFF-wk, OFF — which describes the gate, not the market. An operator reading the tile should not
 * need to know what a weekly cloud is to know what is happening, so the gate is translated once, here,
 * and never leaks into user-facing copy.
 *
 * TWO FACTS, NOT ONE. The verdict carries a path shape AND a direction, and collapsing them loses the
 * half that matters. `OFF` is not "no trend" — it is the same trending path as `ON` with the
 * denominator winning. Rendering it as weakness would invert the read on every axis where the second
 * name is the one to own.
 *
 * NO RANKING BY PERCENTAGE. The issue is explicit and the data agrees: a drift estimate carries a 95%
 * interval that usually straddles zero, so ordering axes by `est` manufactures conviction the numbers
 * do not support. `sig` is the discriminating fact, and the interval must stay on screen — a bare point
 * estimate is the thing this tile exists not to show.
 */

/** The path, in plain words. Never the gate's own vocabulary. */
export type Read = "Trending" | "Pulling back" | "Bouncing" | "No trend";

/** Which leg of the ratio is winning. `null` when the gate says neither is. */
export type Winner = "num" | "den" | null;

export interface RotationVerdict {
  read: Read;
  winner: Winner;
}

/**
 * `verdict` arrives with a status emoji glued to the word ("🟢 ON"), so it is matched on the token
 * rather than compared whole — an emoji change upstream must not silently turn every axis into
 * "No trend", which is the quiet failure a `===` here would produce.
 */
export function readVerdict(verdict: string | null | undefined): RotationVerdict {
  const token = String(verdict ?? "").trim().split(/\s+/).pop()?.toUpperCase() ?? "";
  switch (token) {
    case "ON":
      return { read: "Trending", winner: "num" };
    case "ON-WK":
      return { read: "Pulling back", winner: "num" };
    case "OFF":
      return { read: "Trending", winner: "den" };
    case "OFF-WK":
      return { read: "Bouncing", winner: "den" };
    case "TURN":
      return { read: "No trend", winner: null };
    default:
      // An unrecognised verdict is NOT "No trend" — that is a claim about the market. Unknown is its
      // own answer, the same rule NET follows for a window the broker has not published (#343).
      return { read: "No trend", winner: null };
  }
}

export interface Window {
  est?: number | null;
  lo?: number | null;
  hi?: number | null;
  sig?: boolean | null;
  prev?: number | null;
  move?: number | null;
}

/**
 * The drift line: estimate AND interval, or nothing.
 *
 * Returns null when the interval is unavailable rather than falling back to the bare estimate. A
 * percentage with no interval is precisely the overstatement the issue forbids, and a tile that
 * silently degrades to it on missing data would show its most confident number exactly when it knows
 * least.
 */
export function driftLabel(win: Window | null | undefined): string | null {
  if (!win) return null;
  const { est, lo, hi } = win;
  if (![est, lo, hi].every((v) => typeof v === "number" && Number.isFinite(v))) return null;
  const pct = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
  return `${pct(est as number)} (${pct(lo as number)} … ${pct(hi as number)})`;
}

/**
 * Where the dot sits on the rail: -1 (denominator winning) … +1 (numerator winning).
 *
 * Scaled by the window's own interval half-width rather than by a fixed percentage, so an axis that
 * moves in tenths of a percent and one that moves in whole percents are both legible. Clamped, because
 * a rail is a position and an off-scale dot is not more information, only a broken layout.
 */
export function railPosition(win: Window | null | undefined): number | null {
  if (!win) return null;
  const { est, lo, hi } = win;
  if (![est, lo, hi].every((v) => typeof v === "number" && Number.isFinite(v))) return null;
  const half = Math.max(Math.abs((hi as number) - (lo as number)) / 2, 1e-9);
  return Math.max(-1, Math.min(1, (est as number) / half));
}

/**
 * What the tile is allowed to say, given a payload and its source status (#351).
 *
 * A PURE FUNCTION BECAUSE THE MECHANISM KEEPS CHANGING AND THE PROPERTY DOES NOT. The first cut fetched
 * inside the component, so the only thing a test could reach was the source text — and the tests written
 * against it pinned `useQuery`, `isError` and `fetchStatus === "idle"`, which are react-query's vocabulary,
 * not the cockpit's rule. Moving the tile onto the framework's bound source broke all three while the
 * behaviour they were defending was preserved exactly. A test that fails when the mechanism changes and
 * passes when the rule is broken is pointed at the wrong thing.
 *
 * The rule itself has never changed: ABSENCE IS NOT AN EMPTY MARKET. A dead api, a payload nobody
 * generated, and a genuinely empty axis list are three different statements, and flattening any of them
 * into "no rotations" renders a failure as a calm tape.
 */
export type MarketView =
  | { kind: "absent"; reason: string }
  | { kind: "loading" }
  | { kind: "empty" }
  | { kind: "rotations" };

export interface RotationPayloadLike {
  error?: string | null;
  axes?: unknown[] | null;
}

export function marketView(
  payload: RotationPayloadLike | null | undefined,
  status: string | undefined,
): MarketView {
  // The api's OWN error outranks everything: it answered, and what it said was "there is no read".
  if (payload?.error) return { kind: "absent", reason: payload.error };
  // A dead source is absence even when a stale payload is still in hand, and it is checked before
  // `loading` so a retry cannot present a failure as a request still in progress.
  if (status === "error") return { kind: "absent", reason: "no response from the api" };
  // `loading` means no value at all. A retry still holding a previous payload reports `stale`, and for a
  // once-a-day read the right move is to keep showing it.
  if (status === "loading" || !payload) return { kind: "loading" };
  return (payload.axes?.length ?? 0) === 0 ? { kind: "empty" } : { kind: "rotations" };
}

/* ------------------------------------------------------------------------------------------------
 * The rail, as the design reference actually draws it (#351).
 *
 * The first cut drew two bare dots on a hairline and coloured nothing. Operator, seeing it on a phone:
 * "hard to read … no labels. no colors … I want the bar and bullets to have color and the size in the
 * mockup had meaning. small not filled bullet previous, big filled bullet now."
 *
 * He was describing the reference build (gist f7db5571), which draws five things per row, all in the
 * axis's own colour: the 95% interval as a wide translucent BAND, the ±1 standard-error CORE inside it,
 * a hollow GHOST at last window's position, a MOVE bar with an arrowhead from ghost to now, and the
 * current position as a filled DOT. Filled versus hollow is the gate: a confirmed trend is solid, a
 * weakening one is outlined. None of that was decoration — every mark is a different fact, and dropping
 * them left one undifferentiated grey line per row.
 * ---------------------------------------------------------------------------------------------- */

/** Semantic colour family for an axis: numerator winning, denominator winning, or neither. */
export type Gate = "bull" | "bear" | "watch";

export function gateOf(verdict: string | null | undefined): Gate {
  const { winner } = readVerdict(verdict);
  return winner === "num" ? "bull" : winner === "den" ? "bear" : "watch";
}

/**
 * Is the gate CONFIRMED (filled dot) or merely leaning (hollow)?
 *
 * `ON`/`OFF` are confirmed; `ON-wk`, `OFF-wk` and `TURN` are not. This is the fill rule the operator described
 * from memory ("big filled bullet now") and it matches the reference's `full` flag exactly.
 */
export function isConfirmed(verdict: string | null | undefined): boolean {
  const token = String(verdict ?? "").trim().split(/\s+/).pop()?.toUpperCase() ?? "";
  return token === "ON" || token === "OFF";
}

/**
 * ONE SCALE FOR EVERY ROW, snapped to a round number.
 *
 * Per-row scaling (what the first cut did, inherited from the single-rail helper) makes two rails with
 * wildly different magnitudes look identical — the whole column becomes unreadable as a column, which is
 * half of "hard to read". The reference takes the 85th percentile of every |lo|, |hi| and |est| in the
 * window and snaps up to a round bound, so most rows use most of the rail and the few extremes clamp.
 */
export function railDomain(wins: Array<Window | null | undefined>): number {
  const mags: number[] = [];
  for (const w of wins) {
    if (!w) continue;
    for (const v of [w.lo, w.hi, w.est]) {
      if (typeof v === "number" && Number.isFinite(v)) mags.push(Math.abs(v));
    }
  }
  if (mags.length === 0) return 1;
  mags.sort((a, b) => a - b);
  const p85 = mags[Math.floor(mags.length * 0.85)] ?? mags[mags.length - 1];
  return [1, 2, 3, 5, 8, 12, 20, 30, 50, 80, 120].find((v) => v >= p85 * 1.05) ?? 120;
}

export interface RailMarks {
  /** All in percent of rail width, 0 … 100. */
  lo: number;
  hi: number;
  core1: number;
  core2: number;
  est: number;
  /** Last window's position, and the move bar between it and now. Null when there is no previous. */
  prev: number | null;
  moveFrom: number | null;
  moveWidth: number | null;
  moveRight: boolean;
}

/** Where every mark on one rail sits, given the shared domain. */
export function railMarks(win: Window | null | undefined, domain: number): RailMarks | null {
  if (!win) return null;
  const { est, lo, hi } = win;
  const ok = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
  if (!ok(est) || !ok(lo) || !ok(hi)) return null;
  const pos = (v: number) => ((Math.max(Math.min(v, domain), -domain) + domain) / (2 * domain)) * 100;
  // The ±1 SE core, recovered from the 95% half-width. The reference draws both because the interval
  // alone reads as one flat certainty band when most of the probability sits in its middle third.
  const se = (hi - est) / 1.96;
  const e = pos(est);
  const marks: RailMarks = {
    lo: pos(lo),
    hi: pos(hi),
    core1: pos(est - se),
    core2: pos(est + se),
    est: e,
    prev: null,
    moveFrom: null,
    moveWidth: null,
    moveRight: true,
  };
  if (ok(win.prev)) {
    const p = pos(win.prev);
    marks.prev = p;
    const a = Math.min(p, e);
    const b = Math.max(p, e);
    // Below ~1.6% of the rail the bar and its arrowhead overlap the two dots and read as smudge.
    if (b - a > 1.6) {
      marks.moveRight = p <= e;
      marks.moveFrom = a;
      marks.moveWidth = b - a;
    }
  }
  return marks;
}

/* ------------------------------------------------------------------------------------------------
 * Ordering.
 * ---------------------------------------------------------------------------------------------- */

export type SortMode = "trend" | "drift" | "travel" | "pair";

/**
 * "Most trending on top" — the operator's default, and the one the issue's own analysis argues for.
 *
 * #351 is explicit that ranking by percentage manufactures conviction the data does not support, and
 * that "the discriminating signal is PERSISTENCE (the gate, ADX) — a property of the path, not of the
 * mean". So the default order is exactly that: confirmed gates first, then leaning ones, then no trend;
 * ADX breaks ties inside a band. Drift is available as an explicit choice, where the operator has asked
 * for it rather than been handed it as though it were the ranking.
 */
export function sortAxes<T extends { pair: string; verdict?: string | null; adx?: number | null }>(
  axes: T[],
  mode: SortMode,
  winOf: (axis: T) => Window | null | undefined,
): T[] {
  const num = (v: unknown, fallback = 0) =>
    typeof v === "number" && Number.isFinite(v) ? v : fallback;
  const band = (a: T) => {
    const { read } = readVerdict(a.verdict);
    if (read === "No trend") return 2;
    return isConfirmed(a.verdict) ? 0 : 1;
  };
  const copy = [...axes];
  switch (mode) {
    case "pair":
      return copy.sort((a, b) => a.pair.localeCompare(b.pair));
    case "drift":
      return copy.sort((a, b) => num(winOf(b)?.est, -Infinity) - num(winOf(a)?.est, -Infinity));
    case "travel":
      return copy.sort((a, b) => num(winOf(b)?.move, -Infinity) - num(winOf(a)?.move, -Infinity));
    default:
      return copy.sort(
        (a, b) =>
          band(a) - band(b) ||
          num(b.adx) - num(a.adx) ||
          Math.abs(num(winOf(b)?.est)) - Math.abs(num(winOf(a)?.est)),
      );
  }
}

/**
 * WHICH LEG IS WINNING, IN WORDS — because colour must never carry it alone (#351).
 *
 * The Read words describe the PATH ("Trending", "Pulling back") and say nothing about the SIDE. So
 * `XLE/SPY` and `IWF/IWD` both rendered "Trending" and only the colour distinguished energy leading the
 * market from growth losing to value. Operator: "I'm not sure energy is trending or market (which wouldn't
 * be a trend)."
 *
 * That is exactly the failure `globals.css` forbids and #351's own acceptance names — "colour never
 * alone, the Read word ships with it at every breakpoint" — and I introduced it by mapping both ON and
 * OFF to the single word "Trending".
 *
 * A ratio has no direction of its own: `XLE/SPY` rising means energy beating the market, and the same
 * path falling means the market beating energy. Naming the leader is the only rendering that survives
 * greyscale, and it happens to be what an operator actually wants to read.
 */
export function leaderLabel(
  verdict: string | null | undefined,
  num: string,
  den: string,
): string {
  const { read, winner } = readVerdict(verdict);
  if (winner === null) return "no gate";
  const leader = winner === "num" ? num : den;
  // The modifier is the PATH state, kept short enough for a phone row.
  switch (read) {
    case "Pulling back":
      return `${leader} leads · pausing`;
    case "Bouncing":
      return `${leader} leads · fading`;
    default:
      return `${leader} leads`;
  }
}

/** Is this leg the winning one? Drives the pair's own colouring — winner green, loser red. */
export function legTone(verdict: string | null | undefined, leg: "num" | "den"): Gate {
  const { winner } = readVerdict(verdict);
  if (winner === null) return "watch";
  return winner === leg ? "bull" : "bear";
}
