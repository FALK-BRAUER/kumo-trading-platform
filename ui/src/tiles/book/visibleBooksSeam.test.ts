/**
 * THE SEAM: no production caller may drop the swept per-strategy map (#523).
 *
 * WHAT #523 WAS. A strategy holding nothing has no live trade cycle — a CLOSED cycle is emitted once
 * and dropped from the projection — so it has no book, and its realized P&L had nowhere to render.
 * On 2026-08-24 the panel listed MOMENTUM-002, BCTROT-004 and TECHIVOL-005 while MANUAL-001 was the
 * largest realized contributor in the book and was not on it at all. Re-measured on a live paper
 * stack 2026-08-29, the defect's precondition is still reproducible every day:
 *
 *     live trade cycles -> BCTROT-004, MOMENTUM-002, QC345-003, TECHIVOL-005   (no MANUAL-001)
 *     realized_periods 1M -> MANUAL-001 +1509.72   (2nd largest, behind MOMENTUM-002 +3085.56)
 *
 * The fix gave `visibleBooks` a SECOND, OPTIONAL argument and renders the union of live-cycle and
 * swept strategies. `books.test.ts` pins that union thoroughly — seven tests — and every one of them
 * calls `visibleBooks` directly with a hand-built map.
 *
 * WHICH IS WHY THIS FILE EXISTS. Nothing pinned the CALL SITE. Deleting the second argument leaves the
 * entire suite green while a flat strategy silently disappears from the panel again, because the
 * argument is optional and one of those seven tests deliberately asserts that a caller passing nothing
 * behaves exactly as before: "a change that anchors on something absent does nothing, quietly".
 *
 * WHY A SOURCE-LEVEL ASSERTION. This pins a property of the CODEBASE — that no caller anywhere drops
 * the argument — rather than the behaviour of one render. `flatStrategyRow.test.ts` drives the real
 * component through `renderToString` and is the stronger test of what the panel SHOWS; it cannot,
 * however, speak for a caller nobody has written yet. The two are complementary, and this one is
 * cheap enough to cover every future tile.
 *
 * AIMED AT THE CLASS, NOT THE LINE. It does not assert that any line looks a certain way. It asserts
 * that NO production caller under `src/` calls `visibleBooks` with a single argument, and — because
 * non-empty is not complete — that every module importing it yields at least one call the parser
 * actually RESOLVED. A caller the matcher cannot see is a FAILURE here, not a pass.
 *
 * THE ESCAPES IT HAS ALREADY HAD TO LEARN. A first version matched only the imported name and only a
 * bare identifier callee, so BOTH of these passed all eight tests while dropping the argument:
 *
 *     import { visibleBooks as vb } from ".../books";   vb(attribute(t, []))
 *     import * as B from ".../books";                   B.visibleBooks(attribute(t, []))
 *
 * The first is now resolved through the module's LOCAL BINDING — the alias is what the calls are
 * written in — and the second is banned outright, because a namespace call cannot be told apart from
 * any other property access without full type resolution. Both are exercised below: a guard that
 * recognises its subject by a property the defect destroys can never fire.
 */

import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";

const SRC_ROOT = join(import.meta.dirname, "..", "..");

/** The function whose call sites are under guard, by its EXPORTED name. */
const FN = "visibleBooks";

/** Any import whose specifier names the module that exports it. */
const BOOKS_MODULE = /books$/;

interface CallSite {
  file: string;
  line: number;
  args: number;
}

/** Every `.ts`/`.tsx` under `src/` that is NOT a test — tests call `visibleBooks` one-arg on purpose. */
function productionFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) productionFiles(full, out);
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

function parse(text: string, fileName: string): ts.SourceFile {
  return ts.createSourceFile(
    fileName,
    text,
    ts.ScriptTarget.Latest,
    /* setParentNodes */ true,
    fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
}

function eachImport(sf: ts.SourceFile, visit: (node: ts.ImportDeclaration) => void): void {
  const walk = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node)) visit(node);
    ts.forEachChild(node, walk);
  };
  walk(sf);
}

/**
 * The LOCAL names `visibleBooks` is bound to in this module.
 *
 * Usually just `visibleBooks`, but `import { visibleBooks as vb }` binds it to `vb` and every call is
 * then written `vb(...)`. Matching the EXPORTED name only — which is what `el.name.text` gives for a
 * plain import and what it does NOT give for an aliased one — made the whole guard blind: the module
 * never entered the importer set, so its calls were never looked for and its silence read as clean.
 */
function localBindings(text: string, fileName: string): string[] {
  const names: string[] = [];
  eachImport(parse(text, fileName), (node) => {
    const bindings = node.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) return;
    for (const el of bindings.elements) {
      // `propertyName` is the EXPORTED name and is set only when aliased; `name` is the local binding.
      if ((el.propertyName?.text ?? el.name.text) === FN) names.push(el.name.text);
    }
  });
  return names;
}

/** Namespace imports of the books module — `import * as B from "./books"`. */
function namespaceImports(text: string, fileName: string): string[] {
  const names: string[] = [];
  eachImport(parse(text, fileName), (node) => {
    if (!ts.isStringLiteral(node.moduleSpecifier) || !BOOKS_MODULE.test(node.moduleSpecifier.text)) return;
    const bindings = node.importClause?.namedBindings;
    if (bindings && ts.isNamespaceImport(bindings)) names.push(bindings.name.text);
  });
  return names;
}

/** Calls to any of `names`, with the number of arguments each one actually passes. */
function callsIn(text: string, fileName: string, names: string[]): CallSite[] {
  if (names.length === 0) return [];
  const sf = parse(text, fileName);
  const found: CallSite[] = [];
  const walk = (node: ts.Node): void => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && names.includes(node.expression.text)) {
      found.push({
        file: fileName,
        line: sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1,
        args: node.arguments.length,
      });
    }
    ts.forEachChild(node, walk);
  };
  walk(sf);
  return found;
}

const PRODUCTION = productionFiles(SRC_ROOT).map((file) => ({
  file,
  text: readFileSync(file, "utf8"),
}));
const IMPORTERS = PRODUCTION.map((f) => ({ ...f, bindings: localBindings(f.text, f.file) })).filter(
  (f) => f.bindings.length > 0,
);
const CALL_SITES = IMPORTERS.flatMap((f) => callsIn(f.text, f.file, f.bindings));
const NAMESPACE_IMPORTERS = PRODUCTION.filter((f) => namespaceImports(f.text, f.file).length > 0);

describe("the detector can actually fire", () => {
  // This guard counts ARGUMENTS, so it must be shown to see a one-argument call before its silence
  // means anything — otherwise a parser matching nothing at all would pass every assertion below.

  it("SEES a plain single-argument call and reports it as one argument", () => {
    const src = `import { visibleBooks } from "./books";\nconst rows = visibleBooks(books);`;
    const sites = callsIn(src, "m.ts", localBindings(src, "m.ts"));
    expect(sites).toHaveLength(1);
    expect(sites[0].args).toBe(1);
  });

  it("counts the two-argument call as two, so the two cases are DISTINGUISHABLE", () => {
    // Identical answers for the fixed and broken shapes would make the guard a dead mechanism.
    const src = `import { visibleBooks } from "./books";\nconst r = visibleBooks(books, sweep, unclaimed);`;
    expect(callsIn(src, "m.ts", localBindings(src, "m.ts"))[0].args).toBe(3);
  });

  it("SEES AN ALIASED single-argument call — the escape that shipped green", () => {
    // `import { visibleBooks as vb }` + `vb(x)` passed all eight tests of the first version, because
    // the importer set was keyed on the EXPORTED name and the aliased module never joined it.
    const src = `import { visibleBooks as vb } from "@/tiles/managed-portfolio/books";\nconst r = vb(attribute(t, []));`;
    const bindings = localBindings(src, "m.ts");
    expect(bindings).toEqual(["vb"]);
    const sites = callsIn(src, "m.ts", bindings);
    expect(sites).toHaveLength(1);
    expect(sites[0].args).toBe(1);
  });

  it("SEES a namespace import of the books module — the other escape", () => {
    // `import * as B` + `B.visibleBooks(x)` cannot be told from any other property access without full
    // type resolution, so it is BANNED rather than parsed. The ban is only worth anything if the
    // detector can spot the import, which is what this pins.
    const src = `import * as B from "@/tiles/managed-portfolio/books";\nconst r = B.visibleBooks(x);`;
    expect(namespaceImports(src, "m.ts")).toEqual(["B"]);
  });

  it("does not mistake a same-named PROPERTY call for the bare function", () => {
    // `x.visibleBooks(a)` on some unrelated object is a different callee. Matching it would let an
    // unrelated method silently satisfy — or silently break — this guard. The namespace ban above is
    // what keeps this exclusion from becoming a loophole.
    const src = `import { visibleBooks } from "./books";\nconst r = helper.visibleBooks(books);`;
    expect(callsIn(src, "m.ts", localBindings(src, "m.ts"))).toHaveLength(0);
  });

  it("ignores a module that does not import it at all", () => {
    expect(callsIn("const r = visibleBooks(books);", "m.ts", localBindings("", "m.ts"))).toHaveLength(0);
  });

  it("scanned a real, non-trivial slice of the source tree", () => {
    // Guards against a broken walk that silently returns nothing: if `productionFiles` stopped
    // recursing, every assertion in the next block would pass over an empty set.
    expect(PRODUCTION.length).toBeGreaterThan(50);
  });
});

describe("every production caller passes the swept map (#523)", () => {
  it("finds at least one production call site — the guard is not vacuous", () => {
    expect(CALL_SITES.length).toBeGreaterThan(0);
  });

  it("COVERAGE: every module importing visibleBooks yields a resolved call site", () => {
    // Non-empty is not complete. If a module imports the function but the parser resolves no call in
    // it, the callee has drifted into a shape this matcher cannot see and the guard has gone BLIND
    // rather than green. That must fail and send the next reader here.
    const blind = IMPORTERS.filter((f) => callsIn(f.text, f.file, f.bindings).length === 0).map((f) => f.file);
    expect(blind).toEqual([]);
    expect(IMPORTERS.length).toBeGreaterThan(0);
  });

  it("no production file namespace-imports the books module", () => {
    // BANNED, not parsed. `B.visibleBooks(x)` is indistinguishable from any other property access
    // without a type checker, so allowing the import would reopen the hole this guard exists to close.
    // Import the names directly instead.
    expect(NAMESPACE_IMPORTERS.map((f) => f.file)).toEqual([]);
  });

  it("includes both panels that render per-strategy rows", () => {
    // Named so that deleting a call entirely — rather than just its arguments — also fails, which the
    // argument-count assertion alone would not catch.
    const files = CALL_SITES.map((c) => c.file);
    expect(files.some((f) => f.endsWith("BookTile.tsx"))).toBe(true);
    expect(files.some((f) => f.endsWith("ManagedPortfolioTile.tsx"))).toBe(true);
  });

  it("NO production call site passes fewer than two arguments", () => {
    // THE GUARD. A one-argument call renders the INTERSECTION of live-cycle and swept strategies; the
    // union is the honest set. Dropping the second argument costs the panel MANUAL-001's +1509.72 and
    // says nothing while doing it.
    const underArgued = CALL_SITES.filter((c) => c.args < 2).map((c) => `${c.file}:${c.line}`);
    expect(underArgued).toEqual([]);
  });
});
