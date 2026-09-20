import type { BarDTO } from "@/lib/api/types";

/** Input a checklist condition evaluates against. Framework-generic — knows nothing about any specific
 *  methodology (the ledger's Blue Flag, or any future one); those live entirely in `@/config/checklists.ts`. */
export interface ChecklistContext {
  weeklyBars: BarDTO[];
  dailyBars: BarDTO[];
}

/** One yes/no gate. `evaluate` returns null (not false) when there isn't enough history to judge —
 *  "unknown" and "failed" are different states and must never be conflated into a false conviction. */
export interface ChecklistCondition {
  id: string;
  label: string;
  evaluate: (ctx: ChecklistContext) => boolean | null;
}

/** A named, registrable checklist definition — e.g. the ledger's 8-condition Blue Flag is ONE `ChecklistDef`,
 *  not hardcoded logic in a UI component. A second methodology is a new config entry, not a code change. */
export interface ChecklistDef {
  id: string;
  name: string;
  conditions: ChecklistCondition[];
  /** A hard veto independent of the score tally (e.g. "weekly below cloud" overrides any partial score).
   *  Returns a human-readable reason, or null if nothing vetoes. */
  veto?: (ctx: ChecklistContext) => string | null;
  /** `results` (the per-condition true/false/null array) is passed alongside score/total/veto so a tier
   *  function can inspect a SPECIFIC condition when needed (code review, #181: predecessor-repo's tier branches
   *  on condition 8 specifically when vetoed, not just the aggregate score) — and so it can distinguish
   *  "genuinely failing" from "not enough data to judge yet" (results full of null) rather than the caller
   *  having to infer that from score/total alone, which would otherwise read as an all-fail bearish tier. */
  tier: (score: number, total: number, veto: string | null, results: (boolean | null)[]) => string;
}

export interface ChecklistResult {
  defId: string;
  results: (boolean | null)[];
  score: number;
  total: number;
  /** Count of conditions that returned null — "unknown", not "failed". A tier of all-unknown must read as
   *  insufficient data, never as a bearish score (code review, #181). */
  unknown: number;
  tier: string;
  veto: string | null;
}

export function evaluateChecklist(def: ChecklistDef, ctx: ChecklistContext): ChecklistResult {
  const veto = def.veto?.(ctx) ?? null;
  const results = def.conditions.map((c) => c.evaluate(ctx));
  const score = results.filter((r) => r === true).length;
  const unknown = results.filter((r) => r === null).length;
  const total = def.conditions.length;
  return { defId: def.id, results, score, total, unknown, tier: def.tier(score, total, veto, results), veto };
}
