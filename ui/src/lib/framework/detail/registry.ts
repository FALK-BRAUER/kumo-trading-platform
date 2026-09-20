/**
 * DetailRegistry (#70/#71) — the flexible-extension layer for detail surfaces. A list row taps into a
 * detail whose composition depends on BOTH the focused entity KIND (symbol / order / position) AND the
 * active strategy, resolved by (kind, strategy) — same register→resolve→render pattern as the tile/action
 * registries. Detail variants are pluggable + swappable by id, never hardcoded.
 *
 * Keyed by (focusKind, strategy) — NOT (tab, strategy): an order detail is fundamentally different from a
 * position detail regardless of which tab you opened it from, and strategy composes WITHIN a kind (a
 * MOMENTUM position detail adds the automation panel). Resolution: (kind, strategy) → (kind, "*") → none.
 */
import type { FC } from "react";
import type { Focus, FocusKind } from "@/lib/framework/store";

export interface DetailProps {
  focus: Focus;
  /** The strategy scoping this detail (already resolved; "*" when unknown). */
  strategy: string;
}

export interface DetailDescriptor {
  id: string;
  kind: FocusKind;
  /** The strategy this variant serves, or "*" for the strategy-agnostic default for this kind. */
  strategy: string;
  component: FC<DetailProps>;
  title?: string;
}

const key = (kind: FocusKind, strategy: string): string => `${kind}:${strategy}`;

const REGISTRY = new Map<string, DetailDescriptor>();
const WARNED = new Set<string>(); // dev-warn once per (kind, strategy) fallback — not every render/tick

export function registerDetail(descriptor: DetailDescriptor): void {
  // Idempotent by key — registration runs at module import and may re-run under Next HMR; a second
  // registration of the same (kind, strategy) is a no-op rather than a crash.
  const k = key(descriptor.kind, descriptor.strategy);
  if (REGISTRY.has(k)) return;
  REGISTRY.set(k, descriptor);
}

/** Resolve the descriptor for (kind, strategy): exact match → kind default ("*") → undefined. */
export function resolveDetail(kind: FocusKind, strategy: string): DetailDescriptor | undefined {
  const exact = REGISTRY.get(key(kind, strategy));
  if (exact) return exact;
  const fallback = REGISTRY.get(key(kind, "*"));
  if (fallback && strategy !== "*" && process.env.NODE_ENV === "development") {
    // A missing (kind, strategy) variant silently using the default is a real source of "why is my
    // MOMENTUM detail generic?" bugs — surface it in dev, but ONCE per key (this runs every render/tick).
    const k = key(kind, strategy);
    if (!WARNED.has(k)) {
      WARNED.add(k);
      console.warn(`[DetailRegistry] no ${kind}:${strategy} variant — falling back to ${kind}:*`);
    }
  }
  return fallback;
}

export function detailKeys(): string[] {
  return [...REGISTRY.keys()].sort();
}
