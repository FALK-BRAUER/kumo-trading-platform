/**
 * The SECURED cell's sub-line — ONE derivation, shared with its tooltip (#786).
 *
 * WHY IT IS A FUNCTION. It used to be a ternary inline in `BookTile.tsx` whose last branch fired on
 * `covered > 0` alone, while the tooltip two lines below guarded on `value === 0`. Live result, 30
 * held on alpaca-paper:
 *
 *     SECURED · NOW   $925.85
 *     30 of 30 protected · none above entry
 *
 * Both cannot be true: `securedValue` accumulates ONLY `gain > 0`, so a non-zero value PROVES stops
 * sit above entry. A tally and a caption that disagree about one fact is a safety claim that is
 * wrong on one of the two places showing it — the shape `managed-portfolio/books.ts` already warns
 * about for `isProtected`, here between `sub` and `title` instead.
 *
 * The copy is not the bug and is kept verbatim. It was written for #345, where a fully protected
 * book showed a bare "$0.00" and needed a reason. The reason was right; the guard on WHEN to say it
 * was never added.
 *
 * ORDERING IS DELIBERATE and unchanged: unprotectable and unprotected outrank the locked-in-gain
 * note, because a naked position is a worse fact than an unrealised one.
 */

export interface SecuredTally {
  /** Gain a resting stop would realise if it triggered now. Only positive `(stop − entry) × qty`. */
  value: number;
  covered: number;
  naked: number;
  /** Held positions the engine cannot PRICE, so protection cannot be sized for them at all. */
  unprotectable: number;
  /** Covered holdings whose stop is an ENTRY FLOOR — below entry by construction (#872). */
  floors?: number;
}

/** True when every covered holding still sits at or below its entry — nothing locked in yet. */
export function noneAboveEntry(secured: SecuredTally): boolean {
  return secured.value === 0 && secured.covered > 0;
}

export function securedSub(secured: SecuredTally, held: number): string | undefined {
  // THREE STATES IN THE COPY, because two of them were one number. A count that cannot go down must
  // not read as a backlog that will: staging showed "Unprotected 22" for holdings protection could
  // never cover while the venue withheld their price.
  if (secured.unprotectable > 0 && secured.naked > 0) {
    return `${secured.naked} of ${held} unprotected · ${secured.unprotectable} unpriceable`;
  }
  if (secured.unprotectable > 0) {
    return `${secured.unprotectable} of ${held} unpriceable — cannot be protected`;
  }
  if (secured.naked > 0) {
    return `${secured.naked} of ${held} unprotected`;
  }
  // THE GUARD THAT WAS MISSING. Same predicate the tooltip uses, so the two cannot drift.
  if (noneAboveEntry(secured)) {
    // NAME THE KIND when the stops are entry floors (#872). QC345-003 is protected exactly as
    // approved — a fixed stop at its own entry minus 1.5 x ATR — and a floor sits BELOW entry by
    // construction, so "none above entry" is permanently true there and reads as an alarm. The number
    // does not change; what it MEANS goes on the screen beside it.
    const floors = secured.floors ?? 0;
    if (floors > 0) {
      return `${secured.covered} of ${held} protected · ${floors} floor${floors > 1 ? "s" : ""} below entry`;
    }
    return `${secured.covered} of ${held} protected · none above entry`;
  }
  return undefined;
}
