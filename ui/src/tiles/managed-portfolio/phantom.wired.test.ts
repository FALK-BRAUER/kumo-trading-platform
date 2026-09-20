import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/** The helper is only worth anything if the tile READS it — a green helper test beside an unwired
 *  tile is the seam-versus-unit failure this repo has paid for five times in one day. */
describe("ManagedPortfolioTile reads phantomState for the UNCLAIMED rows", () => {
  const src = readFileSync(new URL("./ManagedPortfolioTile.tsx", import.meta.url), "utf8");
  it("derives the state from the row's venue_qty and gates the click on it", () => {
    expect(src).toMatch(/phantomState\(x\.venue_qty\)/);
    expect(src).toMatch(/!movable\(phantom\)\s*\?\s*undefined/);
  });
  it("renders the label from the same derivation, and the old literal is gone", () => {
    expect(src).toMatch(/phantomLabel\(phantom, originLabel\)/);
    expect(src).not.toContain("— tap to move to a strategy`");
  });
});

describe("a phantom-only book is still SHOWN (#808)", () => {
  const src = readFileSync(new URL("./ManagedPortfolioTile.tsx", import.meta.url), "utf8");
  it("emptiness reads the UNFILTERED external list, the table renders it", () => {
    expect(src).toMatch(/isEmpty=\{groups\.length === 0 && externalAll\.length === 0\}/);
    expect(src).toMatch(/\{externalAll\.map\(\(x\) =>/);
  });
});

describe("one word, one quantity, in BOTH tiles (#808 item 1)", () => {
  const portfolio = readFileSync(new URL("./ManagedPortfolioTile.tsx", import.meta.url), "utf8");
  const book = readFileSync(new URL("../book/BookTile.tsx", import.meta.url), "utf8");
  it("every lane cell's 'standing' is book.unrealized, never book.total", () => {
    for (const src of [portfolio, book]) {
      expect(src).toMatch(/standing \$\{fmtUsd\(book\.unrealized\)\}/);
      expect(src).not.toMatch(/standing \$\{fmtUsd\(book\.total\)\}/);
    }
  });
});
