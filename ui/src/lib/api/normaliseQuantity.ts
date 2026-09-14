/**
 * Make a signed `quantity` unreachable in the UI, at the frame decode (#855).
 *
 * WHY HERE AND NOT AT EVERY READER. The alternative was a scan that enumerates every place a quantity
 * is read, and that list is the thing that keeps being wrong: it cannot see JSX text, function
 * arguments, object literals, or `!==` comparisons, and it has to be re-checked every time someone
 * adds a reader. This repo's own rule from the #511 seeding work applies — when a rule needs a list of
 * everywhere it applies, prefer making the dangerous thing unreachable over keeping the list correct.
 *
 * The expression-level scan in `signedQty.short.test.ts` stays, because it answers a different
 * question: not "could a sign arrive" but "does this arithmetic apply the side correctly". A magnitude
 * multiplied by the wrong direction is still wrong.
 *
 * THE DECODE IS THE CLASS-CLOSER; THE RENDER SITES ARE BELT AND BRACES. Both exist, and the rule for
 * which is which is stated here because the two were inconsistent and a reader could not tell whether
 * a missing `Math.abs()` at a render site was an oversight or a deliberate reliance on this file.
 *
 *   - THE DECODE IS AUTHORITATIVE. Every frame that reaches a tile through `fetchRest`,
 *     `wsManager.onMessage` or a `client.ts` fetcher has had its position-shaped `quantity` fields
 *     absolutised here. Nothing downstream needs to re-check that.
 *   - RENDER SITES STILL TAKE THE MAGNITUDE ANYWAY, and must stay consistent with each other. A
 *     component can be handed a row that never came through a decode: every `*.short.test.ts` renders
 *     one directly, `/dev/ui` mocks do the same, and a future caller may build a row from a store or a
 *     websocket bridge that does not route through here. A component that renders "-10 @ 100.00" when
 *     handed a signed row is wrong on its own terms, regardless of what its usual caller does.
 *   - SO: a render site that shows a quantity takes `Math.abs`, or reads it off `signedQty(...)`. That
 *     is the rule; `ManagedPortfolioTile.tsx:757` was the one site that did not follow it.
 *
 * IT DOES NOT TOUCH `venue_qty`. That field is a THREE-STATE answer from the broker — a number, 0
 * meaning "answered, holds none" (a phantom, #807), and null meaning "not asked yet" — and its sign
 * carries the broker's own direction for a row the cockpit does not own. Absolutising it would make a
 * short holding indistinguishable from a long one at the exact surface built to catch positions the
 * engine has lost track of.
 *
 * IT DEGRADES LOUDLY. `backend/api/test_quantity_is_never_negative.py` pins that no engine writer can
 * emit a negative quantity — every one reads a Nautilus `Quantity`, which refuses values below zero —
 * so this normalisation is expected to be a no-op against today's engine. Expected-to-be-a-no-op is
 * exactly the condition under which a silent repair hides the first producer that breaks the contract,
 * so a repair is COUNTED and the first one WARNS. A fallback that reports nothing is a fallback that
 * turns a contract violation into a rendering detail.
 */

/**
 * The frame keys carrying POSITION-SHAPED rows — a `side` plus a `quantity` describing something held.
 *
 * `fills` and `orders` are deliberately absent, and NOT because those rows carry no quantity: `FillDTO`
 * and `OrderDTO` both do, and `ChartTile` renders a fill's. They are exempt because an ORDER or FILL
 * quantity is a Nautilus `Quantity` at the source — a type that refuses a negative value — and its
 * direction is a BUY/SELL on the order, not a LONG/SHORT on a position. Absolutising them would be
 * defending against a value the type system upstream cannot produce.
 *
 * `normaliseQuantity.short.test.ts` enumerates every `quantity`-bearing DTO in the generated schema and
 * requires each to be either covered here or exempted with a reason, so a new one cannot appear
 * unnoticed.
 */
const ROW_KEYS = ["trades", "positions", "external", "transfers"] as const;

/** How many negative quantities this session has repaired. Read by the test; see IT DEGRADES LOUDLY. */
let repaired = 0;

/** The count of contract violations silently repaired at the decode. Non-zero means a producer emitted
 *  a signed quantity, which `test_quantity_is_never_negative.py` says cannot happen. */
export function normalisedNegatives(): number {
  return repaired;
}

/** Test-only: reset the session counter so one test's repairs cannot be read as another's. */
export function resetNormalisedNegatives(): void {
  repaired = 0;
  warned = false;
}

let warned = false;

/**
 * Absolutise `quantity` on every position-shaped row in a decoded frame, in place.
 *
 * Mutates rather than copies: every caller hands this the result of a `JSON.parse` or a `res.json()`
 * that nothing else holds a reference to, and a deep copy of the whole book on every 2s tick would be
 * a real cost for a defence that is expected to change nothing.
 *
 * Returns its argument so it can wrap a decode expression in one line.
 */
export function normaliseQuantity<T>(frame: T): T {
  if (frame == null || typeof frame !== "object") return frame;
  const obj = frame as Record<string, unknown>;
  for (const key of ROW_KEYS) {
    const rows = obj[key];
    if (!Array.isArray(rows)) continue;
    for (const row of rows) {
      if (row == null || typeof row !== "object") continue;
      const r = row as { quantity?: unknown; instrument_id?: unknown };
      if (typeof r.quantity !== "number" || !Number.isFinite(r.quantity)) continue;
      if (r.quantity >= 0) continue;
      repaired += 1;
      if (!warned) {
        warned = true;
        // ONCE, not per row: a book of forty shorts would otherwise bury the console it is meant to
        // draw attention to. The count is what carries the scale; this carries the fact.
        // eslint-disable-next-line no-console -- see IT DEGRADES LOUDLY in this file's header
        console.warn(
          `[#855] a producer emitted a NEGATIVE quantity on '${key}' (${String(r.instrument_id)}: ` +
            `${r.quantity}). The wire contract is an unsigned quantity with the direction on 'side'. ` +
            `Repairing it here so the UI renders correctly, but this is a contract violation — see ` +
            `backend/api/test_quantity_is_never_negative.py.`,
        );
      }
      r.quantity = Math.abs(r.quantity);
    }
  }
  return frame;
}
