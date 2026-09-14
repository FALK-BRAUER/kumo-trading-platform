"use client";

/**
 * DetailHost (#70/#71) — resolves the active focus through the DetailRegistry and renders the matching
 * detail variant. Replaces Board's hardcoded SymbolDetailSurface render: detail composition is now a
 * function of (focus.kind, strategy), pluggable + swappable by id. Keyed by focus identity so a focus
 * change REMOUNTS (resets local state — no stale ticket against a new entity).
 */
import { useCockpitStore, focusStrategy, focusIdentity } from "@/lib/framework/store";
import { resolveDetail } from "@/lib/framework/detail/registry";
import "@/components/board/detail/registrations"; // side-effect: register the detail variants

export function DetailHost() {
  const focus = useCockpitStore((s) => s.focus);
  const closeDetail = useCockpitStore((s) => s.closeDetail);
  if (!focus) return null;

  const strategy = focusStrategy(focus);
  const descriptor = resolveDetail(focus.kind, strategy);

  if (!descriptor) {
    return (
      <div className="flex h-[calc(100vh-9rem)] flex-col items-center justify-center rounded-xl border border-ds-line bg-ds-surf text-t2">
        <p className="font-mono text-sm">no detail registered for {focus.kind}</p>
        <button onClick={closeDetail} className="mt-3 rounded-md bg-ds-surf2 px-3 py-1.5 font-mono text-xs text-t2">
          close
        </button>
      </div>
    );
  }

  const Detail = descriptor.component;
  return <Detail key={focusIdentity(focus)} focus={focus} strategy={strategy} />;
}
