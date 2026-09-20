/**
 * Ownership-violation wording (#437) — turning `HealthResponse.ownership_violations` into a banner line.
 *
 * DELIBERATELY NOT PART OF `./drift`, and the difference is the whole point. Drift compares the
 * broker against the cache, and every sentence it writes rests on one premise: the broker is the
 * anchor, so trust its number. Here that premise is false. On 2026-08-30 eight mirrored SHORT
 * positions netted to exactly what the broker held — `reconcile_drift` was correctly EMPTY for nine
 * days — while four lanes each mis-stated what they owned by a combined $18,218. The total was
 * right. Both lane figures were wrong.
 *
 * So the remedy is inverted, and a shared banner could not say both things: drift says "trust the
 * broker's number", ownership says "trust NEITHER lane figure until this is reconciled". Folding
 * one into the other would make the banner mean less rather than more — and `classifyDrift` would
 * read a mirrored pair as a side disagreement, which is a different claim again.
 *
 * Nothing in this cockpit shorts, so any negative lane quantity is a booking error on its face,
 * not a position. The usual cause is one lane's exit selling shares another lane owns: under
 * NETTING that closes the seller and mints a short with the excess, stranding the real owner's long.
 */
import type { components } from "@/lib/api/schema";

export type OwnershipViolation = components["schemas"]["OwnershipViolation"];

/** How many lanes to name before eliding — the banner is one line, not a report. */
const MAX_NAMED = 3;

/** And how many symbols per lane. One lane stranded FOUR on 2026-08-30, which is already too wide
 *  for a banner: MOMENTUM-002 held shorts in WHD, CGAU, VCTR and HALO at once. Lanes overflow
 *  rarely, symbols overflow immediately, so both need a cap or the line wraps the header. */
const MAX_SYMBOLS = 3;

/** `AAPL.XNAS` → `AAPL`. The venue suffix is noise in a banner. */
const bare = (instrumentId: string): string => instrumentId.split(".")[0] ?? instrumentId;

/**
 * One line for the banner, or "" when nothing is violating.
 *
 * NAMES THE LANE FIRST, because the lane is what the operator acts on: it is the thing holding a
 * position it cannot exit (a long-only system can only close a short by BUYING, which doubles a
 * real long at market) and the thing whose displayed held size is a lie. The symbols are context.
 */
export function ownershipMessage(violations: OwnershipViolation[] | undefined | null): string {
  if (!violations || violations.length === 0) return "";

  const byLane = new Map<string, string[]>();
  for (const v of violations) {
    const seen = byLane.get(v.strategy_id) ?? [];
    seen.push(bare(v.instrument_id));
    byLane.set(v.strategy_id, seen);
  }

  const lanes = [...byLane.entries()];
  const named = lanes
    .slice(0, MAX_NAMED)
    .map(([lane, symbols]) => {
      const shown = symbols.slice(0, MAX_SYMBOLS).join(", ");
      const extra = symbols.length - MAX_SYMBOLS;
      return `${lane} (${shown}${extra > 0 ? ` +${extra} more` : ""})`;
    })
    .join("; ");
  const rest = lanes.length - MAX_NAMED;
  const tail = rest > 0 ? ` +${rest} more` : "";

  const subject = lanes.length === 1 ? "A lane holds" : `${lanes.length} lanes hold`;
  // "held size is mis-stated" is the operator-facing consequence, and it is deliberately NOT
  // "the broker disagrees" — the broker does not disagree, which is why this went nine days unseen.
  return `${subject} a short in a long-only book — ownership is mis-attributed and every held size ` +
    `below is mis-stated: ${named}${tail}`;
}

/**
 * The book header's ownership badge, THREE-STATED (#884). `null` is the engine not having said
 * (no frame, #859) and must render as unknown; `[]` is asked-and-clean; a non-empty list is the
 * disputed count. Decided here, as a pure function, so a tile cannot collapse the first two.
 */
export type DisputedBadge =
  | { kind: "unknown"; count: null; label: string; title: string }
  | { kind: "none"; count: 0; label: ""; title: "" }
  | { kind: "disputed"; count: number; label: string; title: string };

export function disputedBadge(violations: OwnershipViolation[] | null | undefined): DisputedBadge {
  if (violations === null || violations === undefined) {
    return {
      kind: "unknown",
      count: null,
      label: "· ownership unknown",
      title: "The engine has not reported, so whether any position is booked to a lane holding a short is unknown — not clean.",
    };
  }
  if (violations.length === 0) return { kind: "none", count: 0, label: "", title: "" };
  const n = violations.length;
  return {
    kind: "disputed",
    count: n,
    label: `· ${n} disputed`,
    title:
      `${n} position${n > 1 ? "s" : ""} are booked to a lane that holds a short in a long-only book, so the ` +
      `per-lane split — and this count — describe bookkeeping rather than ownership. The netted book still ` +
      `matches the broker, which is why no drift banner fires. Reconcile ownership before acting on any held size below.`,
  };
}
