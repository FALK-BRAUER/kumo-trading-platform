"use client";

import { useTheme } from "@/lib/useTheme";
import type { ThemeMode } from "@/lib/theme";
import { cn } from "@/lib/utils";

/**
 * Appearance setting (#49) — the client-side theme picker (Light / Dark / System). Rendered in the Settings
 * tile alongside the backend trading domains, but it's local (localStorage), not a server setting.
 */
const OPTIONS: { value: ThemeMode; label: string; glyph: string }[] = [
  { value: "light", label: "Light", glyph: "☀" },
  { value: "dark", label: "Dark", glyph: "☾" },
  { value: "system", label: "System", glyph: "⌘" },
];

export function AppearanceSetting() {
  const { mode, setMode } = useTheme();
  return (
    <div className="rounded-xl border border-ds-line bg-ds-surf p-3">
      <div className="mb-2 font-mono text-[10px] uppercase tracking-wider text-t3">Appearance</div>
      <div className="flex gap-2">
        {OPTIONS.map((o) => (
          <button
            key={o.value}
            type="button"
            onClick={() => setMode(o.value)}
            aria-pressed={mode === o.value}
            className={cn(
              "flex-1 rounded-lg px-3 py-2 font-mono text-xs ring-1 transition-colors",
              mode === o.value
                ? "bg-ds-surf2 text-t1 ring-status-info/50"
                : "text-t2 ring-ds-line hover:text-t1 hover:ring-ds-line2",
            )}
          >
            <span className="mr-1">{o.glyph}</span>
            {o.label}
          </button>
        ))}
      </div>
      <p className="mt-2 font-mono text-[10px] text-t3">System follows your device’s light/dark setting.</p>
    </div>
  );
}
