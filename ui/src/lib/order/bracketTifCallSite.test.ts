/**
 * A guard against #315, aimed at the CLASS of defect rather than the one line that had it.
 *
 * The bug: `StrategyOrderForm` passed a LITERAL `tif: "day"` into `buildBracketPayload`. Alpaca hands the
 * parent bracket's `time_in_force` to both child legs and has no per-leg field, so that literal decided
 * how long the PROTECTIVE STOP lived — it expired at the close and left the position naked until the #239
 * backstop's first regular-hours tick, reopening the overnight gap #239 was built to close.
 *
 * `payload.test.ts` already pins `bracketTif` itself, and it passed with the hardcode fully reintroduced —
 * because the helper was never the problem. The WIRING was. That is the shape which has shipped defects
 * here repeatedly with a green suite, so the assertion has to be about the call site.
 *
 * A source scan rather than a rendered assertion because there is no component-test harness in this repo,
 * and because the invariant is genuinely syntactic: whatever decides a bracket's time-in-force must be the
 * shared rule, not a string typed at the point of use. A future second bracket call site is covered the
 * day it is written.
 *
 * The invariant: **no caller of `buildBracketPayload` may pass a string literal as `tif`.**
 */
import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const SRC = join(__dirname, "..", "..");

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sourceFiles(full));
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

/**
 * The ARGUMENT text of each `buildBracketPayload(...)` call, brace-matched.
 *
 * File granularity was the first attempt and it over-matched: `StrategyOrderForm` also calls
 * `buildOrderPayload` with a literal tif on its non-bracket path, which is a separate question (a plain
 * order carries no protective leg). An invariant that cannot tell those apart reports a defect where
 * there is none, and a test that cries wolf gets deleted.
 */
function bracketCallSites(): { path: string; args: string }[] {
  const out: { path: string; args: string }[] = [];
  for (const path of sourceFiles(SRC)) {
    const text = readFileSync(path, "utf8");
    // Skip the DEFINITION in payload.ts — `export function buildBracketPayload(` is not a call site, and
    // counting it made this test report the helper as its own offender.
    if (/function buildBracketPayload\(/.test(text)) continue;
    let i = text.indexOf("buildBracketPayload(");
    while (i !== -1) {
      let depth = 0;
      let j = i + "buildBracketPayload".length;
      const start = j;
      for (; j < text.length; j++) {
        if (text[j] === "(") depth++;
        else if (text[j] === ")") {
          depth--;
          if (depth === 0) break;
        }
      }
      out.push({ path, args: text.slice(start, j) });
      i = text.indexOf("buildBracketPayload(", j);
    }
  }
  return out;
}

describe("bracket time-in-force is never hardcoded at a call site (#315)", () => {
  // The fixture's own property first. If nothing in the tree calls `buildBracketPayload`, the assertion
  // below is vacuously true and this file would keep passing forever while protecting nothing — the
  // "a test that cannot fail carries no information" rule. Pin the premise.
  it("there IS at least one bracket call site to check", () => {
    const sites = bracketCallSites();
    expect(sites.length).toBeGreaterThan(0);
  });

  it("every bracket call site derives tif from the shared rule, never a literal", () => {
    const offenders: string[] = [];
    for (const { path, args } of bracketCallSites()) {
      const text = readFileSync(path, "utf8");
      // Brace-matched argument text, so a payload spanning many lines is still one unit — the reasoning
      // mobileWidth.test.ts arrived at after a per-line scan missed a bug written across a multiline call.
      if (/\btif:\s*["'`]/.test(args)) {
        offenders.push(`${path.replace(SRC, "src")} passes a literal tif into a bracket payload`);
        continue;
      }
      // Codex review, Medium: the literal check above is trivially evaded by one hop —
      // `const tif = "day"; buildBracketPayload({ ..., tif })`. The value reaching the wire is identical
      // and the assertion never fires. So resolve a bare identifier back to its declaration.
      //
      // `useState("day")` is NOT an offender: that is an operator-changeable control with a default, which
      // is exactly what OrderVanillaTile offers and what the warning covers. A plain `const` initialised
      // from a string literal IS the hardcode, because nothing can change it.
      const shorthand = /\btif\s*[,}]/.test(args) && !/\btif:/.test(args);
      const named = args.match(/\btif:\s*([A-Za-z_$][\w$]*)\s*[,}]/);
      const ident = shorthand ? "tif" : named?.[1];
      if (ident) {
        const frozen = new RegExp(`\\bconst\\s+${ident}\\s*(?::[^=]+)?=\\s*["'\`]`).test(text);
        if (frozen) {
          offenders.push(
            `${path.replace(SRC, "src")} passes '${ident}', a const bound to a string literal, as the bracket tif`,
          );
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  /**
   * The second half, and the one that generalises. Two tickets can place a bracket and they solve the TIF
   * differently on purpose: `StrategyOrderForm` derives it (no control to offer), `OrderVanillaTile` lets
   * the operator choose. Neither is wrong. What WAS wrong is that neither said the choice governs the
   * PROTECTIVE LEG — the stop silently expires at the close, which is indistinguishable on screen from a
   * stop that rests until it fires.
   *
   * So the invariant is not "derive it" but "say what it costs": a screen that can place a bracket must
   * reference `bracketProtectionExpires`. A third ticket added later inherits the requirement.
   */
  it("every screen that can place a bracket tells the operator when the protection expires", () => {
    const silent = [...new Set(bracketCallSites().map((s) => s.path))]
      .filter((path) => {
        // Strip IMPORT statements before looking. The first version of this assertion was satisfied by the
        // import line alone: deleting the warning from the JSX left the suite green, because the
        // identifier still appeared at the top of the file. An unused import is not a warning.
        const body = readFileSync(path, "utf8").replace(/^import[\s\S]*?from\s+["'][^"']+["'];?$/gm, "");
        // And a bare mention is not a warning either (codex review, Medium): the predicate must GATE
        // rendered JSX. `{ … protectionExpiresForTif(x) && (` is the shape that puts words on the screen;
        // a stray call assigned to an unused const would otherwise satisfy this forever.
        return !/\{[^{}]*protectionExpiresForTif\([^)]*\)[^{}]*&&\s*\(/.test(body);
      })
      .map((path) => `${path.replace(SRC, "src")} places a bracket without warning about expiry`);
    expect(silent).toEqual([]);
  });
});
