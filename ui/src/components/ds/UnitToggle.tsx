"use client";

/**
 * The global `$` / `%` toggle (#586; the rule is #392).
 *
 * One control, one answer — the same discipline as `PeriodSelector`, which this deliberately mirrors
 * rather than reinvents. The two are the two axes of one question: *over what window* and *in what
 * unit*. They sit opposite each other on the panel so they read as a pair.
 *
 * IT SWITCHES, IT DOES NOT ADD. Operator, 2026-09-06: "it should not add %. it should switch all relative
 * change fields to %." A figure has one unit at a time, so this changes how a number reads and never
 * how many numbers there are — which is why it costs no row width, the objection that had #392 parked.
 *
 * BALANCES IGNORE IT. Cash and liquidation stay absolute in both states; `BOOK_FIGURES` in `unit.ts`
 * is where that is decided, not here. A control that silently changed a balance would be inventing a
 * denominator.
 */
import { UNITS } from "@/components/ds/unit";
import { useCockpitStore } from "@/lib/framework/store";

export function UnitToggle({ className = "" }: { className?: string }) {
  const unit = useCockpitStore((s) => s.unit);
  const setUnit = useCockpitStore((s) => s.setUnit);

  return (
    <div className={`flex gap-0.5 ${className}`} role="group" aria-label="Value unit">
      {UNITS.map(([key, label]) => (
        <button
          key={key}
          type="button"
          onClick={() => setUnit(key)}
          aria-pressed={key === unit}
          title={
            key === "%"
              ? "Show relative changes as percentages. Balances (cash, liquidation) stay absolute — a percentage of them has no non-circular meaning."
              : "Show relative changes in dollars."
          }
          className={`shrink-0 rounded px-1.5 py-0.5 font-mono text-[11px] transition-colors ${
            key === unit ? "bg-ds-surf2 text-t1" : "text-t3 hover:text-t2"
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
