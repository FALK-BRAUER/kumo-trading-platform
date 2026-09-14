/**
 * A guard against the width regression this UI keeps shipping (#229, #237, #238, #250, #258).
 *
 * The specific defect in #258: the Orders table froze a second column at a HARDCODED horizontal offset —
 * `sticky left-16` — while the first column's width was only ever a hint (`w-16` under `table-layout:
 * auto` is not a constraint). The symbol column measured wider than 64px, so the opaque frozen STATUS
 * cell scrolled over its tail and the SIDE column disappeared underneath it.
 *
 * The invariant, stated so a machine can check it: **in a file that positions anything `sticky`, a
 * nonzero horizontal offset must be breakpoint-gated.** `left-0` assumes nothing about anything's width
 * and is allowed anywhere; `left-16` asserts that whatever sits to its left measures exactly 4rem, which
 * is only ever safe on a viewport wide enough to have been designed for.
 *
 * This is a source scan rather than a rendered assertion because the bug IS the class name — jsdom
 * computes no Tailwind, so a DOM test would pass while the phone stayed broken.
 *
 * Scope, deliberately (codex review): the unit is the FILE, not the line and not the class attribute.
 * Per-line scanning missed the bug written across a multiline `clsx()` call, and matching class
 * attributes properly would need a parser. One file in the codebase pairs `sticky` with any nonzero
 * offset at all, so file granularity costs nothing in noise and cannot be evaded by reformatting.
 */
import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const SRC = join(import.meta.dirname, "..", "..");

/** Only real breakpoints make a hardcoded offset safe. `hover:` and `dark:` emphatically do not. */
const BREAKPOINT = /^(?:sm|md|lg|xl|2xl):/;

/** `sticky` as a whole Tailwind class, not the substring inside `stickyFoo` or a longer word. */
const STICKY = /(?:^|[\s"'`{(,])sticky(?:[\s"'`})\],]|$)/;

/**
 * A horizontal offset with any leading variants: `left-16`, `sm:left-16`, `right-[4rem]`.
 * Arbitrary values are included — `left-[4rem]` is the same assertion `left-16` makes, just spelled to
 * slip past a digits-only pattern (codex review).
 */
const OFFSET = /(?<prefixes>(?:[a-z0-9]+:)*)(?:left|right)-(?<value>\[[^\]]+\]|\d+)/g;

/** `readdirSync` recursion rather than `globSync`, which is not in this project's Node 20 types. */
function tsxFiles(dir: string): string[] {
  const out: string[] = [];
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) out.push(...tsxFiles(p));
    else if (e.name.endsWith(".tsx")) out.push(p);
  }
  return out;
}

/**
 * Comments out. Without this the guard flags the prose explaining it — the fix in `OrdersTile.tsx`
 * documents itself by naming the `left-16` it removed, and this very file does the same (codex review,
 * false positives).
 */
function stripComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
}

describe("mobile width invariants", () => {
  it("finds source to scan", () => {
    expect(tsxFiles(SRC).length).toBeGreaterThan(20);
  });

  it("never pins a nonzero horizontal offset in a file that also positions something sticky", () => {
    const offenders: string[] = [];

    for (const file of tsxFiles(SRC)) {
      const src = stripComments(readFileSync(file, "utf8"));
      if (!STICKY.test(src)) continue;
      for (const m of src.matchAll(OFFSET)) {
        if (m.groups!.value === "0") continue;
        if (BREAKPOINT.test(m.groups!.prefixes ?? "")) continue;
        offenders.push(`${file.slice(SRC.length + 1)}: ${m[0]}`);
      }
    }

    expect(
      offenders,
      "a nonzero sticky offset assumes the width of whatever sits beside it — gate it on sm: and up, " +
        "or use left-0, which assumes nothing",
    ).toEqual([]);
  });
});
