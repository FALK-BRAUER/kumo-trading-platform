"use client";

/**
 * SlideToConfirm (#safety-ux) — the commit gesture. Every action that COMMITS money or DESTROYS state goes
 * through this instead of a plain button: order submit (buy/sell/exit), flatten, cancel, delete, disarm,
 * kill-switch. A tap never fires it.
 *
 * Interaction: drag the thumb past ~90% of the track to fire; released before that snaps back. Pointer AND
 * touch. Keyboard: Enter/Space ARMS ("press again to confirm"), a second Enter/Space fires — never a single
 * key, so focus+Enter can't commit by accident. `prefers-reduced-motion` (or when a drag isn't practical) →
 * the thumb becomes a **press-and-hold ~600ms** confirm with a filling ring. See STYLE_GUIDE component spec.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

export type SlideIntent = "buy" | "sell" | "danger" | "neutral";

const FILL: Record<SlideIntent, string> = {
  buy: "bg-status-bull/25",
  sell: "bg-status-bear/25",
  danger: "bg-status-bear/25",
  neutral: "bg-status-info/25",
};
const THUMB_BG: Record<SlideIntent, string> = {
  buy: "bg-status-bull text-ds-bg",
  sell: "bg-status-bear text-ds-bg",
  danger: "bg-status-bear text-ds-bg",
  neutral: "bg-status-info text-ds-bg",
};

const THUMB = 44; // px — ≥44 touch target
const PAD = 4; // track inset
const COMMIT = 0.9; // fraction of travel to fire
const HOLD_MS = 600;

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

export function SlideToConfirm({
  label,
  onConfirm,
  intent = "neutral",
  disabled = false,
  pending = false,
  reason,
  className,
}: {
  label: string;
  onConfirm: () => void;
  intent?: SlideIntent;
  disabled?: boolean;
  pending?: boolean;
  reason?: string | null;
  className?: string;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const maxRef = useRef(0); // max thumb travel (px)
  const [x, setX] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [armed, setArmed] = useState(false); // keyboard: first key arms, second fires
  const [holdPct, setHoldPct] = useState(0); // reduced-motion press-hold progress
  const holdTimer = useRef<ReturnType<typeof setInterval>>();
  const reduced = useRef(false);

  useEffect(() => {
    reduced.current = prefersReducedMotion();
  }, []);

  const recalc = useCallback(() => {
    const el = trackRef.current;
    if (el) maxRef.current = Math.max(0, el.clientWidth - THUMB - PAD * 2);
  }, []);
  useEffect(() => {
    recalc();
    window.addEventListener("resize", recalc);
    return () => window.removeEventListener("resize", recalc);
  }, [recalc]);

  const locked = disabled || pending;
  const commit = useCallback(() => {
    if (!locked) onConfirm();
  }, [locked, onConfirm]);

  // --- drag (pointer + touch) ---------------------------------------------------------------------
  const onPointerDown = (e: React.PointerEvent) => {
    if (locked) return;
    if (reduced.current) {
      // press-and-hold to confirm
      setHoldPct(0);
      const start = performance.now?.() ?? 0;
      holdTimer.current = setInterval(() => {
        const p = Math.min(1, (((performance.now?.() ?? 0) - start) || 0) / HOLD_MS);
        setHoldPct(p);
        if (p >= 1) {
          clearInterval(holdTimer.current);
          setHoldPct(0);
          commit();
        }
      }, 16);
      return;
    }
    setDragging(true);
    (e.target as Element).setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!dragging) return;
    const el = trackRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    setX(Math.max(0, Math.min(maxRef.current, e.clientX - rect.left - THUMB / 2)));
  };
  const endHold = () => {
    if (holdTimer.current) clearInterval(holdTimer.current);
    setHoldPct(0);
  };
  const onPointerUp = () => {
    if (reduced.current) {
      endHold();
      return;
    }
    if (!dragging) return;
    setDragging(false);
    if (maxRef.current > 0 && x >= maxRef.current * COMMIT) {
      setX(maxRef.current);
      commit();
      setTimeout(() => setX(0), 250);
    } else {
      setX(0); // snap back
    }
  };

  // --- keyboard: two-key arm → confirm ------------------------------------------------------------
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (locked) return;
    if (e.repeat) return; // holding the key must not arm-then-fire on auto-repeat
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      if (armed) {
        setArmed(false);
        commit();
      } else {
        setArmed(true);
      }
    } else if (e.key === "Escape") {
      setArmed(false);
    }
  };

  useEffect(() => () => endHold(), []);

  const fillW = reduced.current ? `${holdPct * 100}%` : `${x + THUMB + PAD}px`;
  // The track is a FIXED-WIDTH control, so a long reason cannot live inside it — an engine rejection is a
  // sentence or two and used to replace the label and clip mid-word, leaving the operator with a fragment
  // and no idea which action it referred to. The track keeps a short state; the reason wraps underneath.
  const shown = pending ? "Sending…" : reason ? "Rejected" : armed ? "Press again to confirm" : label;

  return (
    <div className="w-full">
    <div
      ref={trackRef}
      role="button"
      tabIndex={locked ? -1 : 0}
      aria-label={label}
      aria-disabled={locked}
      aria-busy={pending}
      onKeyDown={onKeyDown}
      onBlur={() => setArmed(false)}
      className={cn(
        "relative h-12 w-full select-none overflow-hidden rounded-xl border border-ds-line2 bg-ds-surf2 outline-none focus-visible:ring-2 focus-visible:ring-status-info/60",
        locked && "cursor-not-allowed opacity-50",
        className,
      )}
    >
      {/* fill behind the thumb */}
      <div
        className={cn("absolute inset-y-0 left-0 rounded-xl transition-[width]", FILL[intent], (dragging || reason) && "transition-none")}
        style={{ width: fillW }}
      />
      {/* label */}
      <span className={cn("pointer-events-none absolute inset-0 flex items-center justify-center px-12 text-center font-mono text-xs font-semibold", reason ? "text-status-bear" : "text-t2")}>
        {shown}
      </span>
      {/* thumb */}
      <div
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        style={reduced.current ? undefined : { transform: `translateX(${x}px)`, transition: dragging ? "none" : "transform 200ms" }}
        className={cn(
          "absolute left-1 top-1 flex h-10 w-11 touch-none items-center justify-center rounded-lg font-mono text-sm font-bold",
          THUMB_BG[intent],
          !locked && "cursor-grab active:cursor-grabbing",
        )}
      >
        {pending ? "…" : "››"}
      </div>
    </div>
      {reason && (
        <p className="mt-1.5 font-mono text-[10px] leading-relaxed text-status-bear">{reason}</p>
      )}
    </div>
  );
}
