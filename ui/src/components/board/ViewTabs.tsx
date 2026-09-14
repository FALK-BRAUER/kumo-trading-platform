"use client";

/**
 * ViewTabs — read-only tab strip to switch between the views defined in the layout config. There is no
 * runtime view management (add/rename/delete) — views live in `@/config/layouts`. A single-view config
 * still renders one (inert) tab. Hidden entirely when there's only one view to avoid chrome noise.
 */
import { useCockpitStore } from "@/lib/framework/store";

export function ViewTabs() {
  const layouts = useCockpitStore((s) => s.layouts);
  const activeViewId = useCockpitStore((s) => s.activeViewId);
  const setActiveView = useCockpitStore((s) => s.setActiveView);

  if (layouts.length < 2) return null;

  return (
    // Six tabs need ~423px; a phone gives 375. Without a scroller the last ones are simply
    // unreachable — Pool clipped, Settings invisible, with no way to get to either. Scroll the STRIP
    // rather than shrinking the labels: a tab you cannot read is no better than one you cannot reach.
    // `shrink-0` on each tab stops flex from compressing them into unreadable slivers instead.
    <div className="mb-3 flex items-center gap-1 overflow-x-auto border-b border-ds-line [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      {layouts.map((view) => {
        const active = view.id === activeViewId;
        return (
          <button
            key={view.id}
            type="button"
            onClick={() => setActiveView(view.id)}
            className={`shrink-0 whitespace-nowrap border-b-2 px-3 py-1.5 font-mono text-[12px] ${
              active
                ? "border-status-bull text-t1"
                : "border-transparent text-t2 hover:text-t1"
            }`}
          >
            {view.name}
          </button>
        );
      })}
    </div>
  );
}
