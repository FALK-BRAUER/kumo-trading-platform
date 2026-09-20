/**
 * Reconciliation drift wording (#195) — turning `HealthResponse.reconcile_drift` into a banner line.
 *
 * Drift has TWO directions and they mean opposite things to the operator. The backend already
 * reports both (`providers/alpaca/exec_client.py` walks the broker's positions, then walks the
 * cache for symbols the broker never mentioned), but the banner used to phrase every entry as
 * "positions held but not shown" and print `broker_qty`. On a phantom — cockpit holds it, broker
 * does not — that renders as "HSBC 0", which states the opposite of what happened and names a
 * quantity nobody holds.
 *
 * So each entry is classified by which side is missing, not assumed:
 *   - UNDERSTATED: the broker holds more than the cockpit shows. An empty book must never be
 *     mistaken for a flat account — this is the original case and stays first, because unshown
 *     risk is worse than shown risk that isn't there.
 *   - PHANTOM: the cockpit shows a position the broker does not have. Inflates the apparent book;
 *     the operator can act on size they do not own.
 *   - MISMATCH: both sides hold the symbol, quantities disagree.
 *
 * Quantities are printed from the side that makes the sentence true: the broker's for understated,
 * the cockpit's for phantom, and both for a mismatch.
 */
import type { components } from "@/lib/api/schema";

export type DriftEntry = components["schemas"]["DriftEntry"];

/** How many symbols to name before eliding — the banner is one line, not a report. */
const MAX_NAMED = 4;

type DriftKind = "understated" | "phantom" | "mismatch" | "insync";

/**
 * `insync` exists so a zero-zero row cannot fall through to a direction. The backend only emits an
 * entry when the two sides actually differ, so this should be unreachable from our own adapter — but
 * classifying it as "understated" would print "SYMBOL 0", which is precisely the meaningless-quantity
 * bug this module was written to remove. A malformed or future producer must not resurrect it.
 */
export function classifyDrift(entry: DriftEntry): DriftKind {
  if (entry.broker_qty === 0 && entry.platform_qty === 0) return "insync";
  if (entry.platform_qty === 0) return "understated";
  if (entry.broker_qty === 0) return "phantom";
  return "mismatch";
}

/**
 * Quantities cross the wire as JSON numbers (the backend casts `Decimal` → `float`), and Alpaca
 * supports fractional shares — so a raw interpolation can surface `0.30000000000000004` or `1e-7`.
 * Fixed-point with the trailing zeros trimmed keeps a share count readable and never goes exponential.
 */
function fmt(n: number): string {
  if (!Number.isFinite(n)) return String(n);
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(8).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}

/**
 * Which way a both-sides-held mismatch cuts. Opposite signs are called out on their own because
 * "broker 50 vs cockpit -50" is a side disagreement, not a size one, and reads as neither by default.
 */
function mismatchDirection(e: DriftEntry): string {
  if (Math.sign(e.broker_qty) !== Math.sign(e.platform_qty)) return "opposite sides";
  return Math.abs(e.broker_qty) > Math.abs(e.platform_qty) ? "broker holds more" : "cockpit shows more";
}

/**
 * Names up to `MAX_NAMED` symbols. The remainder is COUNTED rather than trailing off into an ellipsis:
 * "…" alone hides whether one symbol or two hundred were dropped, and the dropped ones may carry the
 * largest drift.
 */
function name(entries: DriftEntry[], render: (e: DriftEntry) => string): string {
  const shown = entries.slice(0, MAX_NAMED).map(render).join(", ");
  const hidden = entries.length - MAX_NAMED;
  return hidden > 0 ? `${shown}, +${hidden} more` : shown;
}

/**
 * The banner line, or "" when there is no drift. Clauses are ordered by how dangerous the direction
 * is, and only the directions actually present are mentioned.
 */
export function driftMessage(drift: DriftEntry[] | null | undefined): string {
  if (!drift || drift.length === 0) return "";

  const understated = drift.filter((d) => classifyDrift(d) === "understated");
  const phantom = drift.filter((d) => classifyDrift(d) === "phantom");
  const mismatch = drift.filter((d) => classifyDrift(d) === "mismatch");

  const clauses: string[] = [];
  if (understated.length > 0) {
    clauses.push(`held at the broker but not shown: ${name(understated, (e) => `${e.symbol} ${fmt(e.broker_qty)}`)}`);
  }
  if (phantom.length > 0) {
    clauses.push(`shown but not held at the broker: ${name(phantom, (e) => `${e.symbol} ${fmt(e.platform_qty)}`)}`);
  }
  if (mismatch.length > 0) {
    clauses.push(
      `quantity mismatch: ${name(
        mismatch,
        (e) => `${e.symbol} broker ${fmt(e.broker_qty)} vs cockpit ${fmt(e.platform_qty)} (${mismatchDirection(e)})`,
      )}`,
    );
  }
  if (clauses.length === 0) return "";

  return `Broker sync drift — ${clauses.join("; ")}. Manage at your broker.`;
}
