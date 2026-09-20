/**
 * May a leftward drag on this row start a slide-to-delete? (#294)
 *
 * THE DEFECT: scrolling right and deleting were the same gesture. `DataTable` wraps rows in
 * `overflow-x-auto`, and the watchlist is wider than a phone viewport (#250) — so revealing the
 * right-hand columns means dragging the finger LEFT. That is horizontal-dominant with a negative
 * delta, which is precisely what armed the delete. Reading a price could remove the symbol, and no
 * amount of threshold tuning separates them, because the two gestures produce identical deltas.
 *
 * THE DISCRIMINATOR IS THE CONTAINER, NOT THE GESTURE. A delete may only begin when the row is
 * already scrolled fully left, so there is nothing further left to reveal. While hidden content
 * remains to the right, a leftward drag can only be a scroll; once the user is at the left edge, it
 * can only be a delete. That is the iOS convention and it needs no tuning.
 *
 * Extracted from the component rather than inlined so it can be tested against real numbers. jsdom
 * computes no layout — every element reports `scrollWidth === clientWidth === 0` — so a rendered
 * test would exercise the "not scrollable" branch only, and would have passed with the bug present.
 * Taking the measurements as a plain object is what makes both branches reachable.
 */

/** The only three numbers that matter, so a test can state them directly. */
export interface ScrollBox {
  scrollLeft: number;
  scrollWidth: number;
  clientWidth: number;
}

/** Sub-pixel scroll offsets are real on retina; treat anything under a pixel as "at the edge". */
const EPSILON = 1;

export function canScrollHorizontally(box: ScrollBox): boolean {
  return box.scrollWidth - box.clientWidth > EPSILON;
}

/**
 * @param box the nearest horizontally-scrollable ancestor's measurements, or null when there is none
 *
 * A row with NO scrollable ancestor is always swipeable — that is the desktop and narrow-table case,
 * where the gesture was never ambiguous. Returning false there would silently disable delete on
 * every surface that does not scroll.
 */
export function mayStartDelete(box: ScrollBox | null): boolean {
  if (box === null) return true;
  // A container that cannot scroll allows the delete even if it reports a NONZERO offset. That is
  // not hypothetical: filtering the watchlist shrinks the row set, and a browser can leave a stale
  // `scrollLeft` behind after the content no longer overflows. Reading the offset alone would then
  // refuse every delete until something scrolled it back to zero — which nothing can, because there
  // is nothing left to scroll.
  if (!canScrollHorizontally(box)) return true;
  return box.scrollLeft <= EPSILON;
}

/** Walk up for the nearest ancestor that actually scrolls horizontally. */
export function scrollBoxOf(el: Element | null): ScrollBox | null {
  for (let node: Element | null = el; node; node = node.parentElement) {
    const box = { scrollLeft: node.scrollLeft, scrollWidth: node.scrollWidth, clientWidth: node.clientWidth };
    if (canScrollHorizontally(box)) return box;
  }
  return null;
}

/** What the row's touch handler calls. */
export function canSwipeToDelete(el: Element | null): boolean {
  return mayStartDelete(scrollBoxOf(el));
}
