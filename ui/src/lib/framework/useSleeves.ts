/** Each lane's SLEEVE — the denominator its percentages are taken against (#586).
 *
 * A lane card is about the LANE. QC345-003's $1,144.60 is +5.7% on its 20,000 sleeve and +1.1% on the
 * 104,777 account: the first is the lane's performance, the second its contribution to the portfolio.
 * #586 chose the first, so the cell needs the sleeve and nothing else will do — taking the percentage
 * against the account would answer a different question under the same label.
 *
 * `target`, not `actual`. Target is the operator's INTENT and is what the lane is measured against;
 * `actual` is how much capital has been moved into the sleeve so far, and dividing by it would make a
 * lane's return jump every time capital was transferred without any position changing. On 2026-09-05
 * every `actual` on this instance read 0 while every `target` read 20,000 — a denominator that can be
 * zero for a fully-operational lane is not the denominator.
 *
 * ONE READ OF `/strategies` (#888 review): this is a selector over `useStrategyRows`, so the sleeve
 * and the cadence on one cell come from one snapshot — two query keys were two caches of one payload.
 *
 * NULL PER LANE, never a default. `percentOf` renders `—` for a missing or zero sleeve rather than
 * falling back to another base, which is the rule #586 is explicit about: a percentage of the wrong
 * denominator is worse than no percentage.
 */
import { useStrategyRows } from "@/lib/framework/useStrategyRows";

export function useSleeves(): Record<string, number | null> {
  const { rows } = useStrategyRows();
  const out: Record<string, number | null> = {};
  for (const r of rows ?? []) {
    const id = String(r.strategy_id ?? "");
    if (id) out[id] = typeof r.target === "number" ? r.target : null;
  }
  // AN EMPTY MAP ON FAILURE, which renders `—` per lane rather than a wrong percentage. The dollar
  // figures are unaffected: this hook only supplies a denominator.
  return out;
}
