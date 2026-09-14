"use client";

import { cn } from "@/lib/utils";

/**
 * DataTable / DataRow (#104 / #114) — the shared dense list primitive extracted from Watchlist / Orders /
 * Positions. It encodes the row/format contract so every list tile stays consistent and new ones are cheap:
 *
 *  - a symbol-first table with the **first column frozen** (sticky) so identity stays on horizontal scroll,
 *  - a **main row** whose cells align to the column defs, plus
 *  - an **always-visible full-width sub-line** (colSpan) — it is NOT an expander and NOT header-aligned,
 *  - token styling throughout, row tap → the detail surface.
 *
 * The header labels the MAIN row only. Tiles pass the SAME `columns` to DataTable (header) and each DataRow
 * (cell alignment), so header and cells never drift apart.
 */

export interface DataColumn {
  label: string;
  align?: "left" | "right" | "center";
  /** Extra classes for this column's th + td (e.g. "hidden sm:table-cell"). */
  className?: string;
  /** Extra classes for the BODY td only (e.g. tighter padding for an icon-button cell). tailwind-merge
   *  resolves conflicts with the base, so `"px-1 pt-1"` overrides the default `px-2 pt-2`. */
  cellClassName?: string;
}

function alignClass(align?: DataColumn["align"]): string {
  return align === "right" ? "text-right" : align === "center" ? "text-center" : "";
}

export function DataTable({ columns, children }: { columns: DataColumn[]; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-ds-line bg-ds-surf">
      <table className="w-full border-collapse">
        <thead>
          <tr className="bg-ds-surf2/60 text-left font-mono text-[9px] uppercase tracking-wider text-t3">
            {columns.map((c, i) => (
              <th
                key={`${c.label}-${i}`}
                className={cn("px-2 py-1.5", alignClass(c.align), i === 0 && "sticky left-0 z-10 bg-ds-surf2", c.className)}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/**
 * One list entry: a main row (cells aligned to `columns`) + an always-visible sub-line. `className` styles
 * BOTH rows (so terminal dimming / a bracket stripe span the whole entry). The first cell is frozen; supply
 * its content already styled (bold symbol). `sub` is free-form.
 */
export function DataRow({
  columns,
  cells,
  sub,
  onClick,
  className,
  onTouchStart,
  onTouchMove,
  onTouchEnd,
  swipeX,
}: {
  columns: DataColumn[];
  cells: React.ReactNode[];
  sub: React.ReactNode;
  onClick?: () => void;
  className?: string;
  /** Optional touch passthrough on both rows — used for swipe-to-delete (#64). */
  onTouchStart?: React.TouchEventHandler;
  onTouchMove?: React.TouchEventHandler;
  onTouchEnd?: React.TouchEventHandler;
  /** Slide-to-delete: px offset (≤0) the cell contents follow; 0 snaps back with a transition. */
  swipeX?: number;
}) {
  const clickable = onClick ? "cursor-pointer" : "";
  const touch = { onTouchStart, onTouchMove, onTouchEnd };
  // Non-zero = mid-drag (follow the finger, no transition); 0 = released (animate the snap-back).
  const slide: React.CSSProperties | undefined =
    swipeX != null ? { transform: `translateX(${swipeX}px)`, transition: swipeX === 0 ? "transform .2s" : "none" } : undefined;
  return (
    <>
      <tr onClick={onClick} {...touch} className={cn("hover:bg-ds-surf2/40", clickable, className)}>
        {cells.map((cell, i) => (
          <td
            key={i}
            className={cn(
              "px-2 pb-0.5 pt-2 font-mono text-[12px] tabular-nums",
              alignClass(columns[i]?.align),
              columns[i]?.className,
              i === 0 && "sticky left-0 z-10 bg-ds-surf",
              columns[i]?.cellClassName,
            )}
          >
            {slide ? <div style={slide}>{cell}</div> : cell}
          </td>
        ))}
      </tr>
      <tr onClick={onClick} {...touch} className={cn("border-b border-ds-line", clickable, className)}>
        <td colSpan={columns.length} className="px-2 pb-2 pt-0.5 font-mono text-[10px] text-t3">
          {slide ? <div style={slide}>{sub}</div> : sub}
        </td>
      </tr>
    </>
  );
}
