import { cn } from "@/lib/utils";
import { toneForStatus, TONE_CLASS } from "./status";

/**
 * StatusBadge (#104 / #114) — order-lifecycle state as a colour + label badge.
 *
 * Row/format contract: **status is never hidden and colour never alone**. A terminal ERROR
 * (DENIED / REJECTED) renders prominently — filled tint + ring + bold — so a bad order reads at a
 * glance; a user must never scroll or read logs to learn an order failed. Benign lifecycle states
 * (WORKING / ACCEPTED / FILLED) stay quiet. All colours come from the semantic tokens (day/night aware).
 * Tone logic lives in ./status (JSX-free, unit-tested).
 */
export function StatusBadge({ status, className }: { status: string; className?: string }) {
  return (
    <span
      className={cn(
        "inline-block whitespace-nowrap rounded-md px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-wide",
        TONE_CLASS[toneForStatus(status)],
        className,
      )}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}
