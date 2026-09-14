"use client";

/**
 * TileState (#26) — the ONE place a tile's non-content states are rendered: loading, error, empty, stale.
 * Every tile wraps its body in this so the same state looks the same everywhere (no more hand-rolled
 * "positions unavailable" / "No open positions" / "loading…" strings scattered per tile). The tile supplies
 * the domain empty-check (`isEmpty` + `emptyLabel`) — only the tile knows what "empty" means — but the
 * chrome is uniform. `stale` overlays a badge on real content rather than hiding it (last value, flagged).
 */
import type { ReactNode } from "react";
import type { SourceStatus } from "@/lib/framework/types";

function StateBox({ tone, text }: { tone: "error" | "muted"; text: string }) {
  const cls =
    tone === "error"
      ? "border-status-bear/40 bg-status-bear/20 text-status-bear"
      : "border-ds-line text-t3";
  return (
    <div className={`rounded-xl border border-dashed py-12 text-center font-mono text-sm ${cls}`}>{text}</div>
  );
}

export function TileState({
  status,
  isEmpty = false,
  emptyLabel = "No data",
  children,
}: {
  status: SourceStatus;
  isEmpty?: boolean;
  emptyLabel?: string;
  children: ReactNode;
}) {
  if (status === "error") return <StateBox tone="error" text="Unavailable" />;
  if (status === "loading" && isEmpty) return <StateBox tone="muted" text="Loading…" />;
  if (isEmpty) return <StateBox tone="muted" text={emptyLabel} />;
  return (
    <div className="relative">
      {status === "stale" && (
        <span className="absolute right-0 top-0 z-10 rounded-bl rounded-tr bg-status-watch/15 px-1.5 py-0.5 font-mono text-[9px] font-bold text-status-watch">
          STALE
        </span>
      )}
      {children}
    </div>
  );
}
