/**
 * The dropdown must not present an earlier term's matches as the answer to this one (#391).
 *
 * 2026-08-20: "the search result is from last search. it works only once. need to restart."
 *
 * THE ROOT CAUSE IS NOT FIXED HERE AND THESE TESTS DO NOT CLAIM IT IS. Driven through the real React
 * path on desktop Chrome three separate ways — plain retype, retype after the Watch mutation, retype
 * after the Eye/detail action — every query returned correct fresh rows, and the backend answered
 * `q=oii|a|ap|aem` in 3-15ms with correct distinct payloads. Whatever produces the reported behaviour is
 * on the operator's phone and not here. #391 stays open for it.
 *
 * WHAT IS FIXABLE WITHOUT A REPRO IS THE SILENCE. `keepPreviousData` is deliberate — it stops the panel
 * flickering empty between keystrokes — but it means a query that never resolves leaves the previous
 * term's rows on screen, rendered exactly as if they answered what was typed. Same failure as the money
 * planes (#336, #343, #345 item 5): a plausible wrong answer presented as a settled one. Same fix:
 * carry the provenance with the value and say so when they disagree.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { searchPresentation, SLOW_MS } from "./searchPresentation";

const base = { term: "apa", dataTerm: "apa", isFetching: false, pendingMs: 0, hasResults: true };

describe("search staleness is detectable (#391)", () => {
  it("the fixture can express a mismatch", () => {
    // The fixture's own property first: with dataTerm always equal to term, the comparison under test
    // does nothing and every assertion below passes with it deleted.
    expect(searchPresentation({ ...base, dataTerm: "oii" }).kind).not.toBe(
      searchPresentation(base).kind,
    );
  });

  it("rows answering the current term are just fine", () => {
    expect(searchPresentation(base)).toEqual({ kind: "ok" });
  });

  it("rows answering an EARLIER term are reported, and NAME that term", () => {
    // the operator's exact case: typed "apa", looking at "oii". The old term is in the message because "these
    // are not your results" is useless without "…they are the previous ones".
    expect(searchPresentation({ ...base, term: "apa", dataTerm: "oii" })).toEqual({
      kind: "stale",
      showing: "oii",
    });
  });

  it("STALE outranks SLOW when both are true", () => {
    // A pending fetch over an older term's rows is the reported situation exactly. Of the two
    // statements, "what you are looking at is not what you asked for" is the one that changes what the
    // operator does.
    expect(
      searchPresentation({ ...base, dataTerm: "oii", isFetching: true, pendingMs: 9_999 }).kind,
    ).toBe("stale");
  });

  it("a slow fetch admits it only after the threshold", () => {
    expect(searchPresentation({ ...base, hasResults: false, isFetching: true, pendingMs: SLOW_MS - 1 }).kind).toBe("ok");
    expect(searchPresentation({ ...base, hasResults: false, isFetching: true, pendingMs: SLOW_MS }).kind).toBe("slow");
  });

  it("an empty list MID-FETCH is not 'no matches'", () => {
    // "not yet" and "nothing matched" are different claims. Rendering the second while the first is
    // true is the silence-versus-absence confusion in miniature (#298).
    expect(searchPresentation({ ...base, hasResults: false, isFetching: true, pendingMs: 0 }).kind).toBe("ok");
    expect(searchPresentation({ ...base, hasResults: false, isFetching: false }).kind).toBe("empty");
  });

  it("no rows and no provenance is never reported as stale", () => {
    // Nothing rendered means nothing to be wrong about. A stale banner over an empty panel would be
    // noise, and this codebase has shipped alarms that fire on healthy state twice (#387, #390).
    expect(searchPresentation({ ...base, hasResults: false, dataTerm: null, isFetching: false }).kind).toBe("empty");
  });

  it("a missing provenance does not manufacture staleness", () => {
    // Rows from a cache entry written before this change carry no term. Unknown is not stale.
    expect(searchPresentation({ ...base, dataTerm: null }).kind).toBe("ok");
    expect(searchPresentation({ ...base, dataTerm: undefined }).kind).toBe("ok");
  });
});

describe("the component records provenance and renders the states (#391)", () => {
  // COMMENTS STRIPPED — three assertions elsewhere tonight passed against a docstring rather than code.
  const strip = (src: string) => src.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
  const SRC = strip(readFileSync(join(import.meta.dirname, "GlobalSymbolSearch.tsx"), "utf8"));

  it("the scan reads the real component", () => {
    expect(SRC).toContain("GlobalSymbolSearch");
  });

  it("the fetched TERM is stored with the rows", () => {
    // Without this the comparison has nothing to compare against, and the whole guard is inert.
    //
    // Matched inside the QUERY FUNCTION specifically. A first version matched /term:\s*q/ anywhere and
    // passed with the provenance deleted, because `searchPresentation({ term: q, ... })` further down
    // is a legitimate second occurrence. Twelfth test tonight to pass by matching the wrong thing.
    expect(SRC).toMatch(/\(\{\s*term:\s*q,\s*\.\.\.\(await searchInstruments/);
  });

  it("the panel asks the shared predicate rather than re-deriving", () => {
    expect(SRC).toMatch(/searchPresentation\(/);
    // The old inline condition is banned by name: it is what silently rendered stale rows as current.
    expect(SRC).not.toMatch(/results\.length === 0 && !query\.isFetching/);
  });

  it("all three states reach the screen", () => {
    expect(SRC).toMatch(/presentation\.kind === "empty"/);
    expect(SRC).toMatch(/presentation\.kind === "slow"/);
    expect(SRC).toMatch(/presentation\.kind === "stale"/);
  });

  it("the stale banner names the term actually being shown", () => {
    expect(SRC).toMatch(/presentation\.showing/);
  });
});
