"use client";

/**
 * useTheme (#49) — the React hook for the theme setting. Split out from lib/theme.ts (which stays pure so
 * the server component layout.tsx can import the bootstrap script): a server component may not import a
 * module that exports a hook. Reads the stored mode, applies it, and re-applies on OS change in "system".
 */
import { useEffect, useState } from "react";
import { applyThemeClasses, readStoredTheme, storeTheme, DEFAULT_THEME, type ThemeMode } from "./theme";

export function useTheme(): { mode: ThemeMode; setMode: (m: ThemeMode) => void } {
  const [mode, setModeState] = useState<ThemeMode>(DEFAULT_THEME);

  useEffect(() => {
    setModeState(readStoredTheme());
  }, []);

  useEffect(() => {
    applyThemeClasses(mode);
    if (mode !== "system" || typeof window === "undefined") return;
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => applyThemeClasses("system");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [mode]);

  const setMode = (m: ThemeMode) => {
    storeTheme(m);
    setModeState(m);
  };
  return { mode, setMode };
}
