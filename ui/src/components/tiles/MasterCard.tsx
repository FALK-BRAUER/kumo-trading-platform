"use client";

import { ChevronDown, ChevronRight } from "lucide-react";

/**
 * MasterCard — unified 5-zone card row, ported from predecessor-repo/ui.
 *
 * Grid: [3px left bar] [3rem circle] [1fr identity] [auto pill] [6rem value] [auto chevron]
 * Each consumer fills the zones with its own ReactNode content; coordinates never move.
 */

interface MasterCardProps {
  barColorClass: string; // e.g. "border-l-emerald-500"
  circle: React.ReactNode;
  identity: React.ReactNode;
  pill?: React.ReactNode;
  value?: React.ReactNode;
  detailStrip?: React.ReactNode;
  /** Inline-expand state. Omit for a NAVIGATE card (tap routes elsewhere, e.g. to the detail surface #69). */
  expanded?: boolean;
  onToggle: (e: React.MouseEvent) => void;
  expandedPanel?: React.ReactNode;
  dimmed?: boolean;
  paper?: boolean; // amber glow for paper-mode positions
  onRowClick?: (e: React.MouseEvent) => void;
}

export function MasterCard({
  barColorClass,
  circle,
  identity,
  pill,
  value,
  detailStrip,
  expanded = false,
  onToggle,
  expandedPanel,
  dimmed = false,
  paper = false,
  onRowClick,
}: MasterCardProps) {
  return (
    <div
      className={`border-l-[3px] ${barColorClass} ${dimmed ? "opacity-40" : ""} ${
        paper ? "bg-status-watch/10 shadow-[0_0_8px_rgba(245,158,11,0.10)]" : ""
      }`}
    >
      {/* Summary row */}
      <div
        className="grid grid-cols-[3rem_1fr_auto_6rem_auto] items-start gap-x-2.5 px-3 py-2.5 hover:bg-ds-surf2/30 cursor-pointer"
        onClick={onRowClick ?? onToggle}
      >
        <div className="flex items-center justify-center">{circle}</div>
        <div className="min-w-0">{identity}</div>
        <div className="flex items-center">{pill ?? null}</div>
        <div className="text-right">{value ?? null}</div>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onToggle(e);
          }}
          className="text-t3 hover:text-t2 transition-colors p-1"
          aria-label={expanded ? "collapse" : "expand"}
        >
          {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        </button>
      </div>

      {detailStrip && (
        <div className="border-t border-ds-line/25 bg-ds-surf/20 px-3 py-1.5">{detailStrip}</div>
      )}

      {expanded && expandedPanel && (
        <div className="border-t border-ds-line/40">{expandedPanel}</div>
      )}
    </div>
  );
}

/** Standard 52px ring used for the circle zone. */
interface RingProps {
  top: string;
  bottom?: string;
  borderColor: string;
  bgColor: string;
  textColor: string;
}

export function Ring({ top, bottom, borderColor, bgColor, textColor }: RingProps) {
  return (
    <div
      className="w-12 h-12 rounded-full border-2 flex flex-col items-center justify-center flex-shrink-0"
      style={{ borderColor, backgroundColor: bgColor }}
    >
      <span className="font-mono text-xs font-bold leading-none" style={{ color: textColor }}>
        {top}
      </span>
      {bottom && (
        <span
          className="font-mono text-[9px] font-semibold leading-none mt-0.5"
          style={{ color: textColor }}
        >
          {bottom}
        </span>
      )}
    </div>
  );
}
