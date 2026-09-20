/**
 * Drawing OUR OWN activity on the chart (#212, research/alpaca-data-and-charting.md §7).
 *
 * The chart has always shown the market and never shown the system: markers read `BUY 459 @ 21.01`,
 * with no hint of why the strategy acted. The journal already writes that in plain English — "gave
 * back all of a 0.7% peak and is 1.5% below entry" — and the `session` frame now carries it to the
 * UI, so the missing piece was never the words, only the wire.
 *
 * Pure functions, no lightweight-charts imports, so the rules are testable without a canvas.
 *
 * THE ONE RULE THAT MATTERS: an ADOPTED trail has no observed peak, so no peak line is drawn for it.
 * Drawing one would put a fabricated level on the chart in the same visual language as a measured
 * one — and a peak that was never observed is exactly what left the give-back exit unarmed on seven
 * positions (#197 B1). Absence is the honest rendering.
 */
import type { SessionFrame, SessionTrail } from "@/lib/framework/datasource/protocol";

export interface TrailLine {
  price: number;
  /** Rendered as the line's label; kept short because it sits on the price scale. */
  title: string;
  color: string;
  style: "solid" | "dashed";
}

/** Provenances whose peak was actually observed. See `trailLines`. */
const TRUSTWORTHY = new Set(["live", "reconstructed"]);

const ENTRY_COLOR = "#60a5fa";
const PEAK_COLOR = "#a78bfa";

/** The trail row for `symbol`, or null. Symbols are compared case-insensitively and bare (no MIC). */
export function trailFor(frame: SessionFrame | null | undefined,
                         instrumentId: string): SessionTrail | null {
  const symbol = bare(instrumentId);
  return (frame?.trail ?? []).find((t) => bare(t.symbol) === symbol) ?? null;
}

/**
 * Price lines for one instrument's open position: entry always, peak only when it was observed.
 *
 * Returns [] when nothing is held, when the frame has not arrived, or when the numbers are unusable
 * — a chart with no lines reads as "no position", which is true, whereas a line at 0 reads as a
 * level.
 */
export function trailLines(frame: SessionFrame | null | undefined,
                           instrumentId: string): TrailLine[] {
  const t = trailFor(frame, instrumentId);
  if (!t) return [];
  // A trail row OUTLIVES the position it describes. The runner retires a row when it reconciles the
  // claim away, which happens at the START of the next session — so between selling a name and the
  // following morning the row is still there, still carrying the old entry and peak. Observed live:
  // MET and PRU sat at qty 105 and 81 in the trail while the broker held zero of both.
  //
  // Drawing those levels would put an entry and a peak on the chart of a position that no longer
  // exists, which reads as "still held". Correct in the engine (it drops the row before it decides
  // anything), wrong on a chart.
  if (!usable(t.qty ?? null)) return [];
  const out: TrailLine[] = [];
  if (usable(t.entry)) {
    out.push({ price: t.entry as number, title: "entry", color: ENTRY_COLOR, style: "solid" });
  }
  // ALLOW-LIST, not a deny-list. Anything that is not an explicitly trustworthy provenance draws no
  // peak: `adopted`, `ADOPTED`, a missing field, or a value this build has never heard of. A guard
  // written as `!== "adopted"` fails OPEN — every unknown value would draw a fabricated level — and
  // failing open is the wrong direction for the one line that must never be invented.
  if (TRUSTWORTHY.has(String(t.quality ?? "").toLowerCase()) && usable(t.peak) && t.peak !== t.entry) {
    out.push({ price: t.peak as number, title: "peak", color: PEAK_COLOR, style: "dashed" });
  }
  return out;
}

/**
 * Why the strategy exited this symbol, in the journal's own words, or "" when it did not exit or
 * said nothing. Never composed here — the sentence is quoted or it is absent.
 *
 * `fillTsNs` scopes the answer to ONE fill. The frame carries the LATEST decision, which is not
 * necessarily today's, and fills on this chart are filtered by instrument only — so without the date
 * check a sell from three weeks ago, or a manual sell, would be labelled with this morning's reason.
 * Attributing the strategy's rationale to a trade it did not make is the exact class of error this
 * whole area keeps producing, so the reason is withheld unless the fill happened on the session the
 * decision belongs to.
 *
 * Residual, stated rather than hidden: a MANUAL sell of the same symbol on the same session still
 * matches. Fills carry no strategy id, so the UI cannot tell them apart.
 */
export function exitReason(frame: SessionFrame | null | undefined, instrumentId: string,
                           fillTsNs?: number): string {
  const session = frame?.session;
  if (!session) return "";
  if (fillTsNs != null && sessionDate(fillTsNs) !== session) return "";
  const reasons = frame?.decision?.reasons ?? {};
  const symbol = bare(instrumentId);
  const hit = Object.entries(reasons).find(([sym]) => bare(sym) === symbol);
  if (!hit) return "";
  const reason = String(hit[1] ?? "").trim();
  // "entered: rank 4" describes an ENTRY; annotating a sell marker with it would be actively
  // misleading. Only exit reasons belong on a sell.
  return reason.startsWith("entered") ? "" : reason;
}

/** The exchange-local date of a nanosecond timestamp, matching the journal's session labels. */
export function sessionDate(tsNs: number): string {
  const ms = Number(tsNs) / 1e6;
  if (!Number.isFinite(ms)) return "";
  return new Date(ms).toLocaleDateString("en-CA", { timeZone: "America/New_York" });
}

/**
 * Marker label for one fill: what happened, plus why when the journal knows.
 *
 * The reason is appended rather than replacing the price and size — the numbers are what the
 * operator checks against the broker, and the sentence is what explains it.
 */
export function markerLabel(side: string, quantity: number, price: number, reason: string): string {
  const head = `${side} ${quantity} @ ${price}`;
  return reason ? `${head} — ${reason}` : head;
}

/**
 * `AAPL.XNAS` → `AAPL`, and `BRK.B.XNYS` → `BRK.B`.
 *
 * Strips the VENUE from the right, never the first dot-segment: `split(".")[0]` collapses `BRK.B`
 * and `BRK.A` both to `BRK`, so a trail for one class would draw its levels on the other's chart.
 * A MIC is four letters; a share class is one or two, so the distinction is decidable.
 */
function bare(id: string): string {
  const parts = String(id ?? "").trim().toUpperCase().split(".");
  if (parts.length > 1 && /^[A-Z]{4}$/.test(parts[parts.length - 1])) parts.pop();
  return parts.join(".");
}

function usable(v: number | null | undefined): boolean {
  return typeof v === "number" && Number.isFinite(v) && v > 0;
}
