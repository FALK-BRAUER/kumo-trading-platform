/**
 * Theme setting (#49) — a CLIENT-side appearance preference (Light / Dark / System), separate from the
 * backend schema-driven trading settings. Persisted in localStorage; applied by toggling classes on
 * <html>. The cockpit has TWO token systems that flip together: shadcn (`.dark`) and the ds palette
 * (`.day`). So: dark = `.dark`, no `.day`; light = `.day`, no `.dark`. "system" follows the OS.
 *
 * PURE + SSR-safe (no React) so the SERVER component `layout.tsx` can import the bootstrap string. The
 * React `useTheme` hook lives in `./useTheme` ("use client") — a server component may NOT import a module
 * that exports hooks; the RSC bundler rejects it at build time (dev/tsc don't catch it).
 */
import { z } from "zod";

export const themeModeSchema = z.enum(["light", "dark", "system"]);
export type ThemeMode = z.infer<typeof themeModeSchema>;

export const THEME_STORAGE_KEY = "kumo-theme";
export const DEFAULT_THEME: ThemeMode = "dark"; // preserve the current dark-first look until the user opts out

/** Resolve "system" to a concrete light/dark using the OS preference (SSR-safe → dark). */
export function resolveTheme(mode: ThemeMode): "light" | "dark" {
  if (mode !== "system") return mode;
  if (typeof window === "undefined") return "dark";
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** Apply the resolved theme to <html> — toggles the `.dark` (shadcn) and `.day` (ds) classes together. */
export function applyThemeClasses(mode: ThemeMode): void {
  if (typeof document === "undefined") return;
  const light = resolveTheme(mode) === "light";
  const el = document.documentElement;
  el.classList.toggle("day", light);
  el.classList.toggle("dark", !light);
}

export function readStoredTheme(): ThemeMode {
  if (typeof localStorage === "undefined") return DEFAULT_THEME;
  const parsed = themeModeSchema.safeParse(localStorage.getItem(THEME_STORAGE_KEY));
  return parsed.success ? parsed.data : DEFAULT_THEME;
}

export function storeTheme(mode: ThemeMode): void {
  if (typeof localStorage !== "undefined") localStorage.setItem(THEME_STORAGE_KEY, mode);
  applyThemeClasses(mode);
}

/**
 * The no-FOUC bootstrap: an inline script (run in <head> before paint) that reads the stored mode and sets
 * the class synchronously, so a light/system user never flashes the dark default. Kept as a string here so
 * layout.tsx and this module can't drift.
 */
export const THEME_BOOTSTRAP_SCRIPT = `(function(){try{var m=localStorage.getItem('${THEME_STORAGE_KEY}')||'${DEFAULT_THEME}';var light=m==='light'||(m==='system'&&window.matchMedia('(prefers-color-scheme: light)').matches);var e=document.documentElement;e.classList.toggle('day',light);e.classList.toggle('dark',!light);}catch(e){}})();`;
