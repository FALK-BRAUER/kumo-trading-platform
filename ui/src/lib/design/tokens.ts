/**
 * Design-system tokens (#104) — the single JS+CSS source of truth for the cockpit palette.
 *
 * DOM classes (`text-status-bull`, `bg-surf`, …) resolve to the CSS custom properties emitted in
 * `globals.css`; chart colours (lightweight-charts `upColor`/cloud fill, passed in JS) read the raw hex
 * from here. Both derive from the SAME definition, so switching night⇄day re-themes the DOM and the charts
 * together. Values mirror `docs/design-system/mock-v2.html` (the approved reference mock).
 *
 * Semantic status roles (colour never alone — the DOM always carries a text label too):
 *   bull = up / holding-long · bear = down / exiting · watch = staged · info = working · warn = attention.
 */

export type ThemeName = "night" | "day";

export interface ThemeTokens {
  // surfaces
  bg: string;
  bg2: string;
  surf: string;
  surf2: string;
  line: string;
  line2: string;
  // text greys (3 max)
  t1: string;
  t2: string;
  t3: string;
  // semantic status
  bull: string;
  bear: string;
  watch: string;
  info: string;
  warn: string;
  // chart-specific (Ichimoku lines)
  tenkan: string;
  kijun: string;
}

export const THEME_TOKENS: Record<ThemeName, ThemeTokens> = {
  night: {
    bg: "#060608",
    bg2: "#0a0a0d",
    surf: "#101014",
    surf2: "#15151b",
    line: "#1d1d24",
    line2: "#2a2a33",
    t1: "#e6e6ea",
    t2: "#9a9aa4",
    t3: "#63636d",
    bull: "#4ade80",
    bear: "#f47171",
    watch: "#f0b640",
    info: "#5aa9f0",
    warn: "#fb923c",
    tenkan: "#5aa9f0",
    kijun: "#f0b640",
  },
  day: {
    bg: "#f6f6f8",
    bg2: "#eeeef1",
    surf: "#ffffff",
    surf2: "#f3f3f5",
    line: "#e4e4e8",
    line2: "#d3d3d9",
    t1: "#18181b",
    t2: "#52525b",
    t3: "#6b7280",
    bull: "#159a46",
    bear: "#d63030",
    watch: "#c07d12",
    info: "#2563eb",
    warn: "#c2410c",
    tenkan: "#2563eb",
    kijun: "#b7791f",
  },
};

/** Semantic status role → the DOM utility fragment it maps to (used by StatusBadge/DataRow primitives). */
export type StatusRole = "bull" | "bear" | "watch" | "info" | "warn";

export function themeTokens(theme: ThemeName): ThemeTokens {
  return THEME_TOKENS[theme];
}

/**
 * Chart colours for lightweight-charts, resolved from the active theme. Charts take colours as JS values
 * (not CSS), so this is how the chart stays in sync with the DOM tokens across day/night.
 */
export function chartColors(theme: ThemeName) {
  const t = THEME_TOKENS[theme];
  return {
    up: t.bull,
    down: t.bear,
    tenkan: t.tenkan,
    kijun: t.kijun,
    cloudBull: t.bull,
    cloudBear: t.bear,
    text: t.t2,
    grid: t.line,
    background: t.surf,
  };
}

/** Emit the CSS custom-property block for a theme (used to keep globals.css and this file in lock-step). */
export function themeCssVars(theme: ThemeName): Record<string, string> {
  const t = THEME_TOKENS[theme];
  return Object.fromEntries(Object.entries(t).map(([k, v]) => [`--ds-${k}`, v]));
}
