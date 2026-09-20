"use client";

/**
 * The global period selector (#233 follow-up).
 *
 * One control, one answer. Every FLOW figure on screen — realized P&L, period change — answers for the
 * period chosen here, so they reconcile with the equity curve instead of each carrying its own window.
 * STATE figures (cash, deployed, liquidation, unrealized on open positions) deliberately ignore it: a
 * period is meaningless on a balance, and labelling one would imply a history that is not being shown.
 *
 * Design is the equity tile's segmented control, kept rather than reinvented — it is already the
 * vocabulary on the chart, and a second visual language for the same idea is how two controls end up
 * meaning subtly different things.
 */
import { PERIODS } from "@/components/ds/periods";
import { useCockpitStore } from "@/lib/framework/store";

export function PeriodSelector({
  available,
  className = "",
}: {
  /** Periods this surface can actually serve. Others render disabled rather than hidden — a control
   *  that changes shape as data arrives is harder to trust than one that says what it cannot do. */
  available?: Record<string, unknown>;
  className?: string;
}) {
  const period = useCockpitStore((s) => s.period);
  const setPeriod = useCockpitStore((s) => s.setPeriod);

  return (
    <div className={`flex gap-1 ${className}`}>
      {PERIODS.map(([key, label]) => (
        <button
          key={key}
          type="button"
          onClick={() => setPeriod(key)}
          disabled={available ? !available[key] : false}
          aria-pressed={key === period}
          className={`shrink-0 rounded px-2 py-0.5 font-mono text-[11px] transition-colors disabled:opacity-30 ${
            key === period ? "bg-ds-surf2 text-t1" : "text-t3 hover:text-t2"
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
