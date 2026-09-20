/**
 * What the search dropdown is allowed to claim about the rows it is showing (#391).
 *
 * 2026-08-20: "the search result is from last search. it works only once. need to restart."
 *
 * THE ROOT CAUSE IS NOT KNOWN AND THIS DOES NOT CLAIM TO FIX IT. Driven through the real React path on
 * desktop Chrome, three separate ways — plain retype, retype after the Watch mutation, retype after the
 * Eye/detail action — every query returned correct fresh rows. The backend answered `q=oii|a|ap|aem` in
 * 3-15ms with correct distinct payloads. Whatever produces the reported behaviour is present on the operator's
 * phone and absent here, and #391 stays open for it.
 *
 * WHAT IS FIXABLE WITHOUT A REPRO IS THE SILENCE. `placeholderData: keepPreviousData` is deliberate — it
 * stops the dropdown flickering empty between keystrokes — but it means a query that never resolves
 * leaves the PREVIOUS term's rows on screen indefinitely, presented exactly as if they were the answer
 * to what was typed. The panel cannot currently tell the difference, so neither can the operator.
 *
 * That is the same failure this codebase keeps paying for on the money planes — a plausible wrong answer
 * rendered as a settled one (#336, #343, #345 item 5). The fix is the same shape: carry the provenance
 * with the value, and say so when they disagree.
 *
 * HOW STALENESS BECOMES DETECTABLE. The query records the term it fetched INSIDE the cached value, so
 * the rendered rows always know which search they answer. Comparing that against what is currently typed
 * is a fact, not an inference — and it holds whatever the underlying cause turns out to be.
 */

export type SearchPresentation =
  /** Rows answer the current term. */
  | { kind: "ok" }
  /** Rows answer an EARLIER term — say so rather than presenting them as current. */
  | { kind: "stale"; showing: string }
  /** Nothing has come back yet and it is taking long enough to be worth admitting. */
  | { kind: "slow" }
  /** The search ran and genuinely matched nothing. */
  | { kind: "empty" };

/** How long a fetch may be in flight before the panel admits it is waiting. */
export const SLOW_MS = 2_000;

export function searchPresentation(input: {
  /** What is typed right now (already trimmed). */
  term: string;
  /** The term the rendered rows actually answer, or null when nothing is rendered. */
  dataTerm: string | null | undefined;
  isFetching: boolean;
  /** How long the in-flight fetch has been running. Ignored when `isFetching` is false. */
  pendingMs: number;
  hasResults: boolean;
}): SearchPresentation {
  const { term, dataTerm, isFetching, pendingMs, hasResults } = input;

  // STALE FIRST, and it outranks "slow". Both can be true at once — a pending fetch over an older
  // term's rows — and of the two, "what you are looking at is not what you asked for" is the statement
  // that changes what the operator does.
  if (hasResults && dataTerm && dataTerm !== term) return { kind: "stale", showing: dataTerm };

  if (isFetching && pendingMs >= SLOW_MS) return { kind: "slow" };

  // Only once nothing is in flight. An empty list mid-fetch is "not yet", and rendering "no matches"
  // there is the same silence-versus-absence confusion in miniature (#298).
  if (!hasResults && !isFetching) return { kind: "empty" };

  return { kind: "ok" };
}
