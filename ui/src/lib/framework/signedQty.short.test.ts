/**
 * #855, aimed at the CLASS rather than at the reported rows.
 *
 * The sites in the ticket are one defect written several times: a `quantity` whose sign lives on a
 * different field, read into arithmetic or a comparison by whoever happened to need it. Fixing them
 * one at a time leaves the next one — and this scan already found two the ticket does not list
 * (`PositionTransferTile.tsx:62-63`, where the bound is negative and refuses every transfer of a
 * short, and the comparison half of that same expression).
 *
 * So the fix has a shape, not just a list: ONE predicate, `signedQty(side, quantity)`, that takes
 * `Math.abs()` first and then applies `side`. Everything needing a directional quantity calls it;
 * nothing writes the rule out again.
 *
 * THIS TEST FAILS TODAY IN PART BECAUSE `src/lib/framework/signedQty.ts` DOES NOT EXIST. That is
 * deliberate and stated here so the absence cannot be mistaken for a broken import: the module is the
 * intended landing place for the fix, and the first assertion is red until it is written.
 *
 * WHY AN AST AND NOT A REGEX OVER LINES. The first version of this test asked whether the FILE
 * imported the predicate. That is not a check: adding one import at the top of `instrument.ts` would
 * have turned it green while `computePnl` went on multiplying by a raw `position.quantity`. A
 * file-level fact cannot answer an expression-level question, and there is a synthetic test below
 * that pins exactly this — an offending line with the import present is still an offence.
 *
 * The scan therefore asks, of each arithmetic or comparison expression: does THIS OPERAND read a
 * quantity, and is THIS OPERAND normalised? It follows local aliases, because
 * `PositionTransferTile` reaches its quantity through two of them (`qty` from `useState(String(
 * source.quantity))`, then `qtyNum` from `Number(qty)`), and an alias is exactly how a raw read
 * escapes a scan that only looks for the word.
 *
 * Aliasing is deliberately narrow: a declaration inherits the taint only when its initialiser IS the
 * quantity — possibly through `Number`/`String`/`parseFloat`/`parseInt`/`useState` and parentheses —
 * not when it merely mentions one. A wider rule was tried first and tainted thirty unrelated locals
 * in one component, which would have made the offender list unreadable and the test unmaintainable.
 * The cost of the narrow rule is that a quantity laundered through a computed expression is missed;
 * the assertions below are stated so that such a value still has to reach the predicate somewhere.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import ts from "typescript";

const SRC = new URL("../..", import.meta.url).pathname; // ui/src
const PREDICATE = join(SRC, "lib/framework/signedQty.ts");

/**
 * An operand is normalised when the expression ITSELF routes through one of these.
 *
 * `magnitude` is here alongside `Math.abs` because it IS `Math.abs` with a name — leaving it out would
 * report a correctly-written future site (`px * magnitude(p.quantity)`) as an offence, and would let
 * `const size = magnitude(source.quantity)` escape the alias rule instead of being recognised as
 * already normalised.
 */
const NORMALISED = /\b(?:signedQty|magnitude|Math\.abs)\s*\(/;

const ARITHMETIC = new Set([
  ts.SyntaxKind.PlusToken,
  ts.SyntaxKind.MinusToken,
  ts.SyntaxKind.AsteriskToken,
  ts.SyntaxKind.SlashToken,
  ts.SyntaxKind.PlusEqualsToken,
  ts.SyntaxKind.MinusEqualsToken,
  ts.SyntaxKind.AsteriskEqualsToken,
]);
/** Comparisons count. `qtyNum <= source.quantity` places no number at all in a valid range when the
 *  bound is negative, and a scan that only watched arithmetic could not see it. */
const COMPARISON = new Set([
  ts.SyntaxKind.LessThanToken,
  ts.SyntaxKind.LessThanEqualsToken,
  ts.SyntaxKind.GreaterThanToken,
  ts.SyntaxKind.GreaterThanEqualsToken,
]);

export interface Offence {
  line: number;
  /** The offending OPERAND, which is what has to change — not the whole statement. */
  operand: string;
  statement: string;
}

/** Strip the wrappers that pass a quantity through unchanged, so an alias is recognisable. */
function unwrap(text: string): string {
  let prev = "";
  let cur = text.trim();
  while (cur !== prev) {
    prev = cur;
    cur = cur.replace(/^\((.*)\)$/s, "$1").trim();
    cur = cur.replace(/^(?:Number|String|parseFloat|parseInt|useState)\s*\((.*)\)$/s, "$1").trim();
  }
  return cur;
}

const IS_QUANTITY_READ = /^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\.quantity$/;

/** Every arithmetic/comparison operand in `src` that reads a quantity without normalising it. */
export function offences(path: string, src: string): Offence[] {
  const sf = ts.createSourceFile(
    path,
    src,
    ts.ScriptTarget.Latest,
    true,
    /\.tsx$/.test(path) ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );

  const declarations: ts.VariableDeclaration[] = [];
  (function collect(n: ts.Node) {
    if (ts.isVariableDeclaration(n) && n.initializer) declarations.push(n);
    ts.forEachChild(n, collect);
  })(sf);

  // Fixpoint over local aliases. `useState(...)` is destructured, and only element 0 — the value —
  // inherits the taint; the setter is a function and arithmetic on it would be a different bug.
  const aliases = new Set<string>();
  for (let pass = 0; pass < 8; pass++) {
    for (const d of declarations) {
      const init = unwrap(d.initializer!.getText());
      if (!IS_QUANTITY_READ.test(init) && !aliases.has(init)) continue;
      const names = ts.isIdentifier(d.name)
        ? [d.name.text]
        : ts.isArrayBindingPattern(d.name)
          ? [(d.name.elements[0] as ts.BindingElement | undefined)?.name?.getText()].filter(Boolean)
          : [];
      for (const n of names) aliases.add(n as string);
    }
  }

  const readsQuantity = (text: string) =>
    /\.quantity\b/.test(text) || [...aliases].some((a) => new RegExp(`\\b${a}\\b`).test(text));

  const found: Offence[] = [];
  const lineOf = (n: ts.Node) => sf.getLineAndCharacterOfPosition(n.getStart()).line + 1;
  const record = (node: ts.Node, operand: ts.Node) => {
    const text = operand.getText();
    if (!readsQuantity(text) || NORMALISED.test(text)) return;
    found.push({ line: lineOf(node), operand: text, statement: node.getText().split("\n")[0].trim() });
  };

  (function walk(n: ts.Node) {
    if (ts.isBinaryExpression(n) && (ARITHMETIC.has(n.operatorToken.kind) || COMPARISON.has(n.operatorToken.kind))) {
      record(n, n.left);
      record(n, n.right);
    }
    // `-t.quantity` is not a binary expression, and it is the whole of `grouping.ts:77`.
    if (ts.isPrefixUnaryExpression(n) && n.operator === ts.SyntaxKind.MinusToken) record(n, n.operand);
    ts.forEachChild(n, walk);
  })(sf);
  return found;
}

/**
 * Every place the sign rule is WRITTEN OUT instead of called, found structurally rather than by
 * matching one spelling of it.
 *
 * The first version of this check was `/side\s*===\s*"SHORT"\s*\?\s*-/`. It matched three sites and
 * would have been cleared by a fourth copy written `side === "LONG" ? Math.abs(q) : -Math.abs(q)` or
 * `side !== "SHORT" ? ... : ...` — the same rule, the other way round. Banning one spelling of a rule
 * is not banning the rule.
 *
 * So the shape is what is matched: a ternary whose condition mentions `side` (directly, or through a
 * local derived from it — `const long = p.side === "LONG"`) and whose two branches are each other's
 * negation. That is spelling-independent, and it found two copies inside `computePnl` that the regex
 * could not see: `long ? 1 : -1` for the amount and `long ? raw : -raw` for the percent, four lines
 * apart, two derivations of one fact in one function.
 *
 * It deliberately does NOT match `const long = t.side !== "SHORT"` (`books.ts:232`, `:299`). That is a
 * direction predicate used to pick which side of a stop protects a position — a boolean about
 * direction, not a rule about a quantity's sign — and banning it would be a false positive on correct
 * code.
 */
export function handRolledSignRules(path: string, src: string): { line: number; text: string }[] {
  const sf = ts.createSourceFile(
    path,
    src,
    ts.ScriptTarget.Latest,
    true,
    /\.tsx$/.test(path) ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const sideLocals = new Set<string>();
  (function collect(n: ts.Node) {
    if (ts.isVariableDeclaration(n) && n.initializer && ts.isIdentifier(n.name) && /\bside\b/.test(n.initializer.getText())) {
      sideLocals.add(n.name.text);
    }
    ts.forEachChild(n, collect);
  })(sf);
  const mentionsSide = (t: string) =>
    /\bside\b/.test(t) || [...sideLocals].some((l) => new RegExp(`\\b${l}\\b`).test(t));
  const strip = (t: string) => t.trim().replace(/^-\s*/, "").replace(/^\((.*)\)$/s, "$1").trim();

  const out: { line: number; text: string }[] = [];
  (function walk(n: ts.Node) {
    if (ts.isConditionalExpression(n) && mentionsSide(n.condition.getText())) {
      const a = n.whenTrue.getText().trim();
      const b = n.whenFalse.getText().trim();
      if (a.startsWith("-") !== b.startsWith("-") && strip(a) === strip(b)) {
        out.push({
          line: sf.getLineAndCharacterOfPosition(n.getStart()).line + 1,
          text: n.getText().split("\n")[0].trim(),
        });
      }
    }
    ts.forEachChild(n, walk);
  })(sf);
  return out;
}

/** Assignments that PRODUCE a signed quantity. Reading `netQty` in a comparison is fine once it is
 *  correct; writing it is what must go through the predicate. */
export function netQtyWrites(path: string, src: string): { line: number; statement: string }[] {
  const sf = ts.createSourceFile(path, src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const out: { line: number; statement: string }[] = [];
  (function walk(n: ts.Node) {
    if (
      ts.isBinaryExpression(n) &&
      (n.operatorToken.kind === ts.SyntaxKind.EqualsToken ||
        n.operatorToken.kind === ts.SyntaxKind.PlusEqualsToken ||
        n.operatorToken.kind === ts.SyntaxKind.MinusEqualsToken) &&
      /(^|\.)netQty$/.test(n.left.getText())
    ) {
      out.push({
        line: sf.getLineAndCharacterOfPosition(n.getStart()).line + 1,
        statement: n.getText().split("\n")[0].trim(),
      });
    }
    ts.forEachChild(n, walk);
  })(sf);
  return out;
}

/**
 * The file's CODE, with every comment blanked out (offsets preserved).
 *
 * The textual predicates below run over source, and source contains prose. `ichimoku.ts` was reported
 * as doing directional quantity work because a comment explaining this very ticket contained the
 * strings `side === "SHORT"` and `.quantity`. A check that prose can trip is a check that prose can
 * also be written around, and either way it is measuring the wrong thing: a sentence about a rule is
 * not an application of it.
 *
 * Blanked rather than removed so byte offsets and line numbers still line up with the real file.
 */
function stripComments(src: string): string {
  const scanner = ts.createScanner(ts.ScriptTarget.Latest, false, ts.LanguageVariant.JSX, src);
  const chars = src.split("");
  let token = scanner.scan();
  while (token !== ts.SyntaxKind.EndOfFileToken) {
    if (
      token === ts.SyntaxKind.SingleLineCommentTrivia ||
      token === ts.SyntaxKind.MultiLineCommentTrivia
    ) {
      for (let i = scanner.getTokenStart(); i < scanner.getTokenEnd(); i++) {
        if (chars[i] !== "\n") chars[i] = " ";
      }
    }
    token = scanner.scan();
  }
  return chars.join("");
}

/** Every production `.ts`/`.tsx` under `ui/src` — tests and fixtures excluded. */
function productionFiles(dir: string = SRC, acc: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) productionFiles(p, acc);
    else if (/\.tsx?$/.test(name) && !name.includes(".test.") && !name.includes(".fixture.")) acc.push(p);
  }
  return acc;
}

const FILES = productionFiles();
const rel = (p: string) => p.slice(SRC.length).replace(/^\//, "");

/**
 * The ONE exemption, and the measurement behind it.
 *
 * `OrdersTile`'s quantity is an ORDER quantity, not a position quantity, and a Nautilus `Quantity`
 * cannot be negative — measured against the installed package, not remembered:
 *
 *     >>> Quantity(-5, precision=0)
 *     ValueError: invalid `value` less than `QUANTITY_MIN` 0.0, was -5.0
 *
 * So `qN > 0` on an order is a validity check, not a direction check, and routing it through
 * `signedQty` would be wrong rather than merely redundant. Listed by file AND reason so a future
 * reader can re-check the reason rather than trust the list.
 */
const EXEMPT: Record<string, string> = {
  "tiles/orders/OrdersTile.tsx": "OrderDTO.quantity is a Nautilus Quantity — non-negative by construction",
  // The module that DOES the normalising has to read the raw sign to decide whether to repair it, and
  // has to name the value in the warning it emits. Exempt for the same reason `signedQty.ts` is exempt
  // from the sign-rule ban below: a normaliser that normalised its own input could not normalise
  // anything. The staleness guard still applies — if it stops reading a raw quantity, this entry goes.
  "lib/api/normaliseQuantity.ts": "it is the normaliser; inspecting the raw sign is its whole job",
};

/** Offences per file, exemptions removed. */
function offenders(): Record<string, Offence[]> {
  const out: Record<string, Offence[]> = {};
  for (const f of FILES) {
    const found = offences(f, readFileSync(f, "utf8"));
    if (found.length && !(rel(f) in EXEMPT)) out[rel(f)] = found;
  }
  return out;
}

/**
 * THE FOUR SITES AS THEY WERE WRITTEN, VERBATIM, KEPT AS FIXTURES.
 *
 * This guard used to assert that the scan still found these in the LIVE TREE. That worked exactly
 * once: the moment the fix landed the sites were gone, and the guard could no longer distinguish "the
 * detector works and the tree is clean" from "the detector has been narrowed until it matches
 * nothing" — which is the precise failure it exists to prevent, and which this repo has shipped (an
 * AST scan matching only `ast.Name` callees found three sites and missed both production lanes).
 *
 * So the corpus moves into the test. The detector is driven over the code that WAS wrong, which
 * cannot be silently fixed out from under it, and the guard keeps its meaning for as long as the file
 * exists. Each snippet is the real line, with the file and line it came from named beside it.
 */
const KNOWN_BAD: { site: string; src: string; operand: string }[] = [
  {
    site: "components/board/detail/PositionDetail.tsx:246 (market value)",
    src: `const mktValue = price != null ? price * p.quantity : null;`,
    operand: "p.quantity",
  },
  {
    site: "lib/framework/instrument.ts:253 (computePnl amount)",
    src: `const amt = (price - basis) * position.quantity * (long ? 1 : -1);`,
    operand: "position.quantity",
  },
  {
    site: "tiles/managed-portfolio/grouping.ts:77 (netQty)",
    src: `g.netQty += t.side === "SHORT" ? -t.quantity : t.quantity;`,
    operand: "t.quantity",
  },
  {
    // Two aliases deep and used as a BOUND, not in arithmetic — the shape a word-matching scan misses.
    site: "tiles/position-transfer/PositionTransferTile.tsx:57-63 (aliased bound)",
    src: [
      `const [qty, setQty] = useState(String(source.quantity));`,
      `const qtyNum = Number(qty);`,
      `const qtyOk = Number.isFinite(qtyNum) && qtyNum > 0 && qtyNum <= source.quantity;`,
    ].join("\n"),
    operand: "qtyNum",
  },
];

describe("the scan can see, and can be made blind", () => {
  it("it walks the real tree, not an empty one", () => {
    // A scan that resolved the wrong root would report a clean codebase and pass everything below.
    expect(FILES.length).toBeGreaterThan(100);
    expect(FILES.map(rel)).toContain("lib/framework/instrument.ts");
  });

  it("it still finds every site that was known to be wrong", () => {
    // The vacuity guard that matters — see the note on KNOWN_BAD for why the corpus lives here rather
    // than in the tree it was fixed out of.
    for (const c of KNOWN_BAD) {
      const found = offences("a.tsx", c.src);
      expect(found.length, `${c.site}: the scan reported nothing`).toBeGreaterThan(0);
      expect(found.map((f) => f.operand), c.site).toContain(c.operand);
    }
  });

  it("each of those sites goes quiet once it routes through the predicate", () => {
    // The other half, and the one that says the detector is a fix TARGET rather than a permanent
    // alarm: the same four lines, written the way they are written now, report nothing.
    const fixed = [
      `const mktValue = price != null ? price * signedQty(p.side, p.quantity) : null;`,
      `const amt = (price - basis) * signedQty(position.side, position.quantity);`,
      `g.netQty += signedQty(t.side, t.quantity);`,
      [
        `const size = magnitude(source.quantity);`,
        `const [qty, setQty] = useState(String(size));`,
        `const qtyNum = Number(qty);`,
        `const qtyOk = Number.isFinite(qtyNum) && qtyNum > 0 && qtyNum <= size;`,
      ].join("\n"),
    ];
    for (const src of fixed) expect(offences("a.tsx", src), src).toEqual([]);
  });

  it("A FILE-LEVEL IMPORT DOES NOT CLEAR AN OFFENDING LINE", () => {
    // The defect in the first version of this test, pinned so it cannot come back. Importing the
    // predicate and then not calling it is the single easiest way to make a file-level check green
    // while the arithmetic is untouched.
    const withImport = [
      `import { signedQty } from "@/lib/framework/signedQty";`,
      `export const v = (p: any, px: number) => px * p.quantity;`,
    ].join("\n");
    expect(offences("a.ts", withImport)).toHaveLength(1);
    expect(offences("a.ts", withImport)[0].operand).toBe("p.quantity");
  });

  it("`magnitude(...)` counts as normalised, in an operand and in an alias", () => {
    // `magnitude` is the module's named `Math.abs`. A scan that did not know it would report the fix
    // as the defect — and `const size = magnitude(source.quantity)` is exactly how the transfer tile
    // is written now.
    expect(offences("a.ts", `const v = px * magnitude(p.quantity);`)).toHaveLength(0);
    expect(
      offences("a.ts", [`const size = magnitude(source.quantity);`, `const ok = n <= size;`].join("\n")),
    ).toHaveLength(0);
    // And it is not a blanket amnesty on the line: a raw read beside a normalised one still reports.
    expect(offences("a.ts", `const v = magnitude(a.quantity) * b.quantity;`)).toHaveLength(1);
  });

  it("it goes quiet only when the EXPRESSION is normalised", () => {
    // The exemption has to be real or the offenders assertion is unfixable; and it has to be narrow
    // or it is a way to hide.
    expect(offences("a.ts", `const v = px * p.quantity;`)).toHaveLength(1);
    expect(offences("a.ts", `const v = px * Math.abs(p.quantity);`)).toHaveLength(0);
    expect(offences("a.ts", `const v = px * signedQty(p.side, p.quantity);`)).toHaveLength(0);
  });

  it("it follows local aliases and it watches comparisons", () => {
    // Both are how `PositionTransferTile` hides: the quantity arrives through two aliases and is
    // then used as a BOUND rather than in arithmetic.
    const aliased = [
      `const [qty] = useState(String(source.quantity));`,
      `const qtyNum = Number(qty);`,
      `const ok = qtyNum > 0 && qtyNum <= source.quantity;`,
    ].join("\n");
    const found = offences("a.tsx", aliased);
    expect(found.map((f) => f.operand)).toContain("qtyNum");
    expect(found.map((f) => f.operand)).toContain("source.quantity");
  });

  it("prose is not code — a comment about the rule is not an application of it", () => {
    // `ichimoku.ts` was reported as doing directional quantity work because a comment explaining this
    // ticket contained `side === "SHORT"` and `.quantity`. A scan prose can trip is a scan prose can
    // be written around.
    const commented = [
      `// applies side === "SHORT" to a raw .quantity, which is the bug`,
      `/* const v = px * p.quantity; */`,
      `export const v = 1;`,
    ].join("\n");
    expect(stripComments(commented)).not.toMatch(/\.quantity\b/);
    expect(offences("a.ts", commented)).toHaveLength(0);
    // And it does not blind the scan to real code on a commented line.
    expect(offences("a.ts", `const v = px * p.quantity; // a comment`)).toHaveLength(1);
  });

  it("it does not taint a local that merely MENTIONS a quantity", () => {
    // The over-broad version of aliasing tainted thirty locals in one component. An unreadable
    // offender list is a list nobody maintains, which is the same failure as no list at all.
    expect(offences("a.ts", `const label = \`\${p.quantity} shares\`;\nconst n = label.length * 2;`)).toHaveLength(0);
  });

  it("every exemption is still earning its place", () => {
    // A stale exemption is a hole that opens silently. If an exempted file stops offending, the
    // entry must go — otherwise it sits there ready to cover a real defect added later.
    for (const path of Object.keys(EXEMPT)) {
      const full = FILES.find((f) => rel(f) === path);
      expect(full, `${path} is exempted but no longer exists`).toBeTruthy();
      expect(offences(full!, readFileSync(full!, "utf8")).length, `${path} no longer offends`).toBeGreaterThan(0);
    }
  });
});

describe("one predicate owns the sign (#855)", () => {
  it("`signedQty` exists where the fix is meant to land", () => {
    // RED BY ABSENCE, deliberately: `ui/src/lib/framework/signedQty.ts` is not in the tree yet.
    expect(existsSync(PREDICATE)).toBe(true);
    expect(readFileSync(PREDICATE, "utf8")).toMatch(/export function signedQty\s*\(/);
  });

  it("no expression does directional quantity arithmetic or comparison unnormalised", () => {
    // Reported as file -> operands, so the failure names the expressions to change rather than the
    // files to look through.
    const found = offenders();
    const summary = Object.fromEntries(
      Object.entries(found).map(([f, os]) => [f, os.map((o) => `${o.line}: ${o.operand}`)]),
    );
    expect(summary).toEqual({});
  });

  it("a signed quantity is only ever PRODUCED by the predicate", () => {
    // `netQty` is the tile's directional quantity. Reading it is fine once it is right; writing it
    // from a raw `quantity` is the defect, and it is invisible to the operand scan above whenever
    // the right-hand side happens to be normalised in one branch and not the other.
    const writes: string[] = [];
    for (const f of FILES) {
      const src = readFileSync(f, "utf8");
      for (const w of netQtyWrites(f, src)) {
        if (!NORMALISED.test(w.statement) && !/signedQty/.test(w.statement)) {
          writes.push(`${rel(f)}:${w.line} ${w.statement}`);
        }
      }
    }
    expect(writes).toEqual([]);
  });

  it("the sign rule is written down exactly once, in any spelling", () => {
    // MEASURED, five copies across four files: `books.ts:424` and `PositionDetail.tsx:248` spell it
    // correctly with `Math.abs`; `grouping.ts:77` omits it and is wrong; and `instrument.ts:253`
    // and `:256` are two more, four lines apart in one function, one for the amount and one for the
    // percent. Four right and one wrong is exactly the condition under which the wrong one is
    // invisible. After the fix the only file allowed to contain the shape is the predicate.
    const writers: string[] = [];
    for (const f of FILES) {
      for (const r of handRolledSignRules(f, readFileSync(f, "utf8"))) {
        if (rel(f) !== "lib/framework/signedQty.ts") writers.push(`${rel(f)}:${r.line} ${r.text}`);
      }
    }
    expect(writers).toEqual([]);
  });

  it("every file doing directional quantity work imports the predicate", () => {
    // THE POSITIVE HALF. The expression-level check above says no line does it raw; this says the
    // files that do it at all go through one module. Neither is sufficient alone — a file-level
    // import cannot see an unfixed line (pinned above), and an expression-level check cannot see a
    // file that quietly reimplements the rule under another name.
    //
    // "Directional quantity work" = the file reads a `.quantity` AND compares a `side` to LONG or
    // SHORT. That is both halves of the pair the predicate exists to join, and it stays a non-empty
    // set after the fix — a definition keyed on the defect would go vacuous the moment it landed.
    //
    // ON THE NAME COLLISION (`PositionDetail.tsx:248` declares `const signedQty`): THE LOCAL GOES.
    // It is one of the five hand-rolled copies the test above bans, so the fix deletes it and there
    // is nothing left to shadow. The matcher accepts an aliased import all the same, so this test
    // does not dictate naming anywhere it has no business doing so.
    const IMPORTS_PREDICATE = /import\s*\{[^}]*\bsignedQty\b[^}]*\}\s*from\s*["'][^"']*signedQty["']/;
    const missing = FILES.filter((f) => {
      const src = stripComments(readFileSync(f, "utf8"));
      const directional = /\.quantity\b/.test(src) && /\bside\s*(?:===|!==)\s*["'](?:LONG|SHORT)["']/.test(src);
      return directional && rel(f) !== "lib/framework/signedQty.ts" && !IMPORTS_PREDICATE.test(src);
    }).map(rel);
    expect(missing).toEqual([]);
  });
});
