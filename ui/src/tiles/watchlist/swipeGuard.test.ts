/**
 * #294 — scrolling right and deleting were the same gesture.
 *
 * The watchlist row sits inside `DataTable`'s `overflow-x-auto` and is wider than a phone (#250), so
 * revealing the right-hand columns drags the finger LEFT: horizontal-dominant, negative delta, which
 * is exactly what armed slide-to-delete. Reading a price could remove the symbol.
 *
 * These take measurements as plain numbers on purpose. jsdom computes no layout — every element
 * reports `scrollWidth === clientWidth === 0` — so a rendered test reaches only the "not scrollable"
 * branch and would have passed with the bug present.
 */
import { describe, it, expect } from "vitest";
import { canScrollHorizontally, mayStartDelete, type ScrollBox } from "./swipeGuard";

const box = (scrollLeft: number, scrollWidth: number, clientWidth: number): ScrollBox => ({
  scrollLeft,
  scrollWidth,
  clientWidth,
});

describe("#294 slide-to-delete must not fire while the row is being scrolled", () => {
  it("REFUSES a delete while there is still content to the right", () => {
    // The live case: a 640px-wide row in a 390px iPhone viewport, scrolled to the left edge... no —
    // scrolled left edge is allowed. This is mid-scroll, which is where the symbol vanished.
    expect(mayStartDelete(box(120, 640, 390))).toBe(false);
  });

  it("ALLOWS a delete once scrolled fully left — nothing further to reveal", () => {
    expect(mayStartDelete(box(0, 640, 390))).toBe(true);
  });

  it("allows a delete when the row does not scroll at all", () => {
    // Desktop and narrow tables, where the gesture was never ambiguous. Returning false here would
    // silently disable delete on every surface that fits.
    expect(mayStartDelete(box(0, 390, 390))).toBe(true);
  });

  it("allows a delete on a non-scrollable row that reports a STALE offset", () => {
    // Filtering the watchlist shrinks the row set, and a browser can leave scrollLeft behind after
    // the content stops overflowing. Reading the offset alone would refuse every delete from then
    // on — nothing can scroll it back, because there is nothing left to scroll.
    expect(mayStartDelete(box(5, 390, 390))).toBe(true);
  });

  it("allows a delete when there is no scrollable ancestor", () => {
    expect(mayStartDelete(null)).toBe(true);
  });

  it("tolerates sub-pixel scroll offsets, which are real on retina", () => {
    expect(mayStartDelete(box(0.5, 640, 390))).toBe(true);
    expect(mayStartDelete(box(4, 640, 390))).toBe(false);
  });

  it("does not treat a sub-pixel width difference as scrollable", () => {
    expect(canScrollHorizontally(box(0, 390.4, 390))).toBe(false);
    expect(canScrollHorizontally(box(0, 640, 390))).toBe(true);
  });

  it("the FIXTURE can distinguish the two cases at all", () => {
    // Guards against the test that cannot fail: if a scrolled and an unscrolled box gave the same
    // answer, every assertion above would be measuring nothing.
    expect(mayStartDelete(box(120, 640, 390))).not.toBe(mayStartDelete(box(0, 640, 390)));
  });
});

describe("the row actually consults the guard", () => {
  it("WatchlistTile gates the swipe on canSwipeToDelete", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const src = readFileSync(join(__dirname, "WatchlistTile.tsx"), "utf8");
    const line = src.split("\n").find((l) => /d\.swiping = true/.test(l) || /canSwipeToDelete/.test(l));
    expect(line, "the swipe is never armed at all").toBeTruthy();
    // The seam: a correct helper nobody calls is the shape that has broken this repo repeatedly.
    expect(src).toContain("canSwipeToDelete(e.currentTarget)");
  });
});
