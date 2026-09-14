/**
 * EVERY TILE THAT RENDERS A LANE CELL OBEYS THE $ / % TOGGLE (#392; the Book panel is #586).
 *
 * WHAT WENT WRONG. Operator, from the phone on 2026-09-14 06:45Z, against staging2: *"2 tabs disagree on
 * momentum p&l"*. With `%` selected, Home read `MOMENTUM-002 -0.0%` and Portfolio read
 * `MOMENTUM-002 -$12.87`. They did not disagree. Checked against the served terms of that window
 * (`GET /pnl/unrealized-base`, 1W: `mv_base 0.0`, `invested 100,469.77`) and the lane's own sleeve
 * (`GET /strategies`, `target` = 100,000), −12.87 IS −0.013%, which `fmtPct` renders as `-0.0%`. Four
 * lanes out of four agreed to the cent. ONE value, TWO units, and nothing on either screen saying which.
 *
 * WHY THE EXISTING GUARD DID NOT SEE IT. `tiles/book/unitSwitchWiring.test.ts` is the class guard for
 * this rule and it opens ONE file:
 *
 *     const code = readFileSync(join(__dirname, "BookTile.tsx"), "utf8");
 *
 * Its own header names the risk — *"a correct helper nothing routes through is the defect with extra
 * steps... #606 wired into one of two providers"* — and then scopes itself to a single provider. The
 * second lane cell lives in `ManagedPortfolioTile.tsx`, was never in its field of view, and rendered
 * `fmtUsd` unconditionally for the eight days the toggle has existed.
 *
 * SO THIS GUARD TAKES THE CLASS AS ITS SUBJECT. It does not name the tiles; it DISCOVERS them, by the
 * one thing a lane cell cannot fake — a call to `cellHeadline`, the shared helper that decides what a
 * lane cell's largest number means. A third tile rendering a lane cell joins this test the moment it
 * is written, which is the only version of this guard that could not have been passed by the defect it
 * was written for.
 */

import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

/** Every `.tsx` under `src`, recursively. Tests are excluded: they QUOTE the calls below.
 *
 * `src`, not `src/tiles` (peer review, S1). A lane cell is not required to live under `tiles/` — the
 * discovery predicate below is what defines the class, and anchoring it to a directory as well would
 * reintroduce, one level coarser, exactly the "the guard only looks where the defect was not" failure
 * this file exists to replace. Measured when widened: no `.tsx` outside `src/tiles` calls
 * `cellHeadline` today, so it adds no members and changes no result — it removes a future blind spot,
 * at no cost now.
 */
function reactSources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      out.push(...reactSources(path));
      continue;
    }
    if (entry.endsWith(".tsx") && !entry.includes(".test.")) out.push(path);
  }
  return out;
}

/**
 * THE CLASS, DISCOVERED RATHER THAN LISTED. A lane cell is a component that asks `cellHeadline` what
 * its headline means — every one of them renders the same three figures against the same lane, so
 * every one of them owes the same unit rule. A hard-coded list is what let the second tile drift.
 */
const laneCellTiles = reactSources(join(__dirname, "..")).filter((p) =>
  /\bcellHeadline\(/.test(readFileSync(p, "utf8")),
);

describe("the lane cell class", () => {
  it("has more than one member — a guard that discovers only its own tile is the defect again", () => {
    // #392 existed for eight days as a rule ONE file obeyed. If this ever reads 1, either the class
    // collapsed or the discovery above stopped matching — both mean this file is no longer guarding
    // anything, and a green run would be a lie.
    expect(laneCellTiles.length).toBeGreaterThan(1);
    const names = laneCellTiles.map((p) => p.split("/").pop());
    expect(names).toContain("BookTile.tsx");
    expect(names).toContain("ManagedPortfolioTile.tsx");
  });
});

describe.each(laneCellTiles.map((p) => [p.split("/").pop() as string, p] as const))(
  "%s obeys the unit toggle",
  (_name, path) => {
    const code = readFileSync(path, "utf8");

    it("reads the GLOBAL toggle, rather than deciding the unit locally", () => {
      // One store, every screen (#392's cross-screen rule). A tile holding its own unit state would
      // make the two tabs disagree in the other direction, which is the same bug wearing a hat.
      expect(code).toMatch(/useCockpitStore\(\(\w+\) => \w+\.unit\)/);
    });

    it("renders its switching figures through the SHARED helper, not a local formatter", () => {
      // `laneFigure` owns "dollars, or a percentage of this lane's sleeve, or `—` when no honest
      // percentage exists". Two copies of that decision is exactly how the two cells drifted.
      expect(code).toMatch(/import \{[^}]*\blaneFigure\b[^}]*\} from "@\/components\/ds\/unit"/);
      expect(code).toMatch(/laneFigure\(\s*\w+,\s*unit,\s*sleeve\s*\)/);
    });

    it("switches the HEADLINE and the window REALIZED — the two relative-change figures", () => {
      // The headline is the lane's window net of flows (#699 a) and REALIZED is its FIFO half. Both
      // are returns on the lane, both take the sleeve as denominator (`BOOK_FIGURES.strategyValue`,
      // `.strategyRealized`), so both switch.
      expect(code).toMatch(/cellUnit\(head\.value\)/);
      expect(code).toMatch(/cellUnit\(periodRealized\)/);
    });

    it("leaves what does NOT answer the selector absolute — standing, the day move, the mark", () => {
      // THE RULE IS NOT "LEVELS STAY ABSOLUTE" (peer review, point 3). `mark` is a delta, the same
      // shape as `real`, and `real` switches — so "level vs delta" does not divide these. What
      // divides them is which question the figure answers: THE FIGURES THAT ANSWER THE SELECTOR
      // SWITCH, THE CARRIED CONTEXT DOES NOT. The headline and the window realized are the lane's
      // return over the selected window; `standing` is a level the selector does not govern, and
      // `day` and `mark` are carried BESIDE the headline, deliberately not summed into it (#699) —
      // rendering them in the headline's unit would invite reading them as part of it.
      //
      // Whether `day` and `mark` should switch against their OWN basis is a real question and it is
      // filed, not decided here. Parity with BookTile is what this asserts today; parity is not a
      // reason, which is why the reason is written above it.
      // NOT EVERY CELL CARRIES ALL THREE — `ManagedPortfolioTile` renders no `· mark` fragment at
      // all; its `unrealizedDelta` only feeds `cellHeadline`. So a missing marker is legitimate.
      // But "no marker matched" and "every marker passed" must not look the same (peer review, S3):
      // a renamed fragment would empty this loop and the test would go green having asserted
      // nothing, which is this file's own origin story. So count the matches and require one.
      const lines = code.split("\n");
      const matched = [/standing \$\{/, /day \{fmtUsd\(/, /" · mark "/]
        .map((marker) => lines.find((l) => marker.test(l)))
        .filter((l): l is string => l !== undefined);
      expect(matched.length, "no level or carried figure found — the markers moved and this guard is blind").toBeGreaterThan(0);
      for (const line of matched) {
        expect(line, `a level or carried figure routes through the switch: ${line.trim()}`).not.toMatch(
          /cellUnit\(|laneFigure\(/,
        );
      }
    });

    it("takes the percentage against THIS LANE'S sleeve, never the account", () => {
      // QC345-003's $1,144.60 is +5.7% of its sleeve and +1.1% of the account: the first answers
      // "how did this lane do", the second "what did it contribute". The cell is about the lane
      // (#586). `useSleeves` is the one source; a locally derived denominator answers the other
      // question under the same label.
      expect(code).toMatch(/useSleeves\(\)/);
    });
  },
);
