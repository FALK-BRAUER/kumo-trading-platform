/**
 * The ONE place the quantity sign rule is written (#855).
 *
 * THE WIRE CONTRACT: `quantity` is a MAGNITUDE and the direction lives on `side`. Every engine writer
 * reads a Nautilus `Quantity`, which refuses a negative value — measured, and pinned by
 * `backend/api/test_quantity_is_never_negative.py`, so no producer emits a sign today.
 *
 * That contract was never enforced on the reading side, and the rule for reassembling the two halves
 * was written out by hand in five places across four files. Four of them were correct. That is exactly
 * the condition under which the fifth being wrong is invisible: `grouping.ts` omitted the `Math.abs()`
 * and nothing could disagree with it, because nothing ever handed it a signed quantity to disagree
 * about. Two of the five sat four lines apart inside `computePnl`, one for the amount and one for the
 * percent, so a change to either could silently make the headline report a gain beside a loss.
 *
 * MAGNITUDE FIRST, THEN THE SIDE. That ordering is the whole content of the fix. Applying `side` to a
 * value that already carries a sign negates it twice and cancels; taking the magnitude first makes the
 * function idempotent in the spelling of its input, so `signedQty("SHORT", 10)` and
 * `signedQty("SHORT", -10)` are both −10 and no caller has to know which spelling it was handed.
 *
 * THE DETECTOR/DISPLAY SPLIT. There is a second, deliberately different predicate on the backend:
 * `api/ownership.py::signed_qty_of` applies the side to the RAW value, no `abs()`, because it exists to
 * DETECT a contract violation — a row claiming LONG while holding −28 must read as a violation, not be
 * silently repaired. This module is the DISPLAY half, and its backend twin is
 * `api/ownership.py::display_signed_qty`. They answer `LONG/-28` differently ON PURPOSE, and a backend
 * test pins that they do. Never merge them: a detector that normalises its input cannot detect
 * anything, and a display that raises on a bad input blanks the book.
 *
 * RETURNS, NEVER RAISES, for the same reason its backend twin does: these run inside render paths and
 * inside the trade-cycle publisher, where a raise does not surface a bad quantity — it removes the
 * whole book from the screen. An unparseable quantity yields 0, which renders as "flat" and is visibly
 * wrong, rather than as an empty book, which is indistinguishable from a liquidated one.
 */

/** The size, with any sign discarded. Non-finite input yields 0 — see RETURNS, NEVER RAISES above. */
export function magnitude(quantity: number): number {
  return Number.isFinite(quantity) ? Math.abs(quantity) : 0;
}

/** +1 for a long, −1 for a short, 0 for a flat or unknown side. The direction on its own, for the
 *  places that need to point a value that is NOT a quantity — a percentage move, say — the same way
 *  the position points. Kept here so the mapping from `side` to a sign exists once. */
export function sideSign(side: string): 1 | -1 | 0 {
  if (side === "SHORT") return -1;
  if (side === "LONG") return 1;
  return 0; // FLAT, and anything the wire has not taught us about
}

/**
 * The position's quantity as a signed number: negative short, positive long, 0 flat.
 *
 * `signedQty(side, quantity) === sideSign(side) * magnitude(quantity)` — stated as one expression so
 * the two exports cannot drift apart into the pair of derivations this module exists to end.
 */
export function signedQty(side: string, quantity: number): number {
  return sideSign(side) * magnitude(quantity);
}
