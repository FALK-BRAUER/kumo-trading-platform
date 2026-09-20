/**
 * ONE PARSER FOR THE RENDERED STRATEGY CELLS, shared by every test that reads them.
 *
 * WHY IT IS SHARED. There were three near-identical copies — `windowHeadline`, `bookCellHeadline`,
 * `flatStrategyRow` — and three consecutive commits had to touch all three. Two of them grew an
 * identical `text` field by hand in the same change. That is the drift argument made concrete, and
 * the collision below is a bug that would otherwise have needed fixing in each copy separately.
 *
 * WHAT EACH CALLER KEEPS. Its own "the parser actually resolves cells" guard. A silently-empty
 * shared parser is the one failure that would make this consolidation dangerous — it would report a
 * clean panel and a broken matcher identically, in every file at once — so every file proves it can
 * see cells before trusting anything it says about the numbers.
 *
 * THE SUB-LINE IS ANCHORED POSITIONALLY, NOT BY CLASS, and that is deliberate. `tint(0)` returns
 * `text-t3`, so a day move of exactly $0.00 renders the day line with a class byte-identical to the
 * sub-line's — and the day line comes FIRST in the DOM. A class-anchored regex takes the first match
 * and would silently read "day $0.00" as the sub-line, making every sub assertion describe the wrong
 * element. Not reachable with today's fixture (its day move is 38.88) and entirely reachable with a
 * flat one, which is a perfectly ordinary thing to want to test. Taking the LAST `text-[10px]` block
 * in the cell cannot collide that way.
 */

export type RenderedCell = {
  label: string;
  /** The big number. `—` when the window has not been swept. */
  headline: string;
  /** The bottom line: held count, what the headline is, and the standing level. */
  sub: string;
  /** The WHOLE cell, tags stripped — the day line lives here and in neither field above. */
  text: string;
};

/**
 * @param headlineClass `text-sm` on ManagedPortfolioTile, `text-base` on BookTile. The only real
 *   difference between the three copies this replaces; a parameter, not a reason for a copy.
 * @param panelStart optional marker to slice the panel out first, so a position table below cannot
 *   contribute cells (ManagedPortfolioTile renders one; BookTile does not).
 */
export function renderedCells(
  html: string,
  headlineClass: "text-sm" | "text-base",
  panelStart?: string,
): RenderedCell[] {
  let clean = html.replace(/<!-- -->/g, "");
  if (panelStart) {
    const start = clean.indexOf(panelStart);
    if (start < 0) return [];
    const end = clean.indexOf('<div class="relative">');
    clean = clean.slice(start, end < 0 ? undefined : end);
  }
  const out: RenderedCell[] = [];
  for (const raw of clean.split('class="bg-surf px-3 py-2"').slice(1)) {
    // CUT AT THE FOOTER. The LAST cell's chunk runs past the grid into whatever follows it, and
    // BookTile's "unattributed" footer is itself a `font-mono text-[10px] text-t3` div — so the
    // positional anchor below happily captured IT as that cell's sub-line. Demonstrated at `all`:
    // the Unclaimed cell (the #596 row, of all of them) parsed with
    // sub = "unattributed $0.14 · All ?". Nothing read a last-cell sub yet, so nothing was wrong —
    // and the first test that did would have silently described the footer.
    //
    // Same species as the tint(0) collision this parser was extracted to fix, which is the argument
    // for having ONE parser: this is one edit rather than three.
    const cut = raw.indexOf('class="mt-1 px-1');
    const chunk = cut < 0 ? raw : raw.slice(0, cut);
    const label = /tracking-wider text-t3">([^<]+)</.exec(chunk)?.[1];
    // `[^>]*>` and not `[^"]*">`: the headline carries a `title` attribute after its class, so the
    // tag does not end at the closing quote. Anchoring there made an earlier copy of this parser
    // match NOTHING the moment the element gained an attribute.
    const headline = new RegExp(`font-mono ${headlineClass}[^>]*>([^<]*)`).exec(chunk)?.[1];
    if (!label || headline === undefined) continue;
    const tenPx = [...chunk.matchAll(/font-mono text-\[10px\][^>]*>([\s\S]*?)<\/div>/g)];
    const sub = tenPx.length ? tenPx[tenPx.length - 1][1] : "";
    out.push({
      label,
      headline: headline.trim(),
      sub: sub.replace(/<[^>]*>/g, "").trim(),
      text: chunk.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim(),
    });
  }
  return out;
}

export const cellNamed = (cells: RenderedCell[], label: string) => cells.find((c) => c.label === label);
