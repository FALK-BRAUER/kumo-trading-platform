/**
 * PEAK (#46) arm-time parameters.
 *
 * These used to be hardcoded here. They now come from the `peak` settings domain
 * (`backend/config/settings/peak.schema.json`), edited on the Settings tab like every other domain —
 * Operator, 2026-08-13: "for the percentages there should be a dedicated setting for this automation
 * following our settings approach".
 *
 * The values below remain as the FALLBACK, used when the settings call has not resolved yet or fails.
 * They are the schema's own defaults; if the two ever drift, the schema wins at runtime and this only
 * covers the gap before the first response. Arming with a stale default would be worse than briefly
 * disabling the toggle, so `usePeakParams` reports whether it is still waiting.
 *
 * Two unit systems meet here, deliberately:
 *   - SETTINGS are in PERCENT, because that is what a person reading "wide trail 2.5%" thinks in, and
 *     it is the unit Alpaca states its own 0.1% minimum in.
 *   - ENGINE PARAMS take trail widths in BASIS POINTS (`trail_wide_bps`), matching the Nautilus
 *     trailing-offset type the order actually carries.
 * `peakParamsFrom` is the single place that converts, so the conversion cannot drift between callers.
 */
import { useQuery } from "@tanstack/react-query";
import { getSettings } from "@/lib/api/client";

/** Engine-facing arm params — basis points for trails, percent for the signal thresholds. */
export interface PeakDefaults {
  trail_wide_bps: number;
  trail_tight_bps: number;
  ext_pct_threshold: number;
  slope_pct_threshold: number;
  off_hod_pct_threshold: number;
  lower_high_bars: number;
  trim_max: number;
  trim_fraction: number;
}

/** Operator-facing settings — percent throughout. Mirrors `peak.schema.json`. */
export interface PeakSettings {
  trailWidePct: number;
  trailTightPct: number;
  extPctThreshold: number;
  slopePctThreshold: number;
  offHodPctThreshold: number;
  lowerHighBars: number;
  trimMax: number;
  trimFraction: number;
}

export const PEAK_SETTINGS_FALLBACK: PeakSettings = {
  trailWidePct: 2.5,
  trailTightPct: 1.0,
  extPctThreshold: 40,
  slopePctThreshold: 3,
  offHodPctThreshold: 2,
  lowerHighBars: 3,
  trimMax: 2,
  trimFraction: 0.45,
};

/** Percent → basis points. One place, so the two unit systems can never disagree. */
export function peakParamsFrom(s: PeakSettings): PeakDefaults {
  return {
    trail_wide_bps: Math.round(s.trailWidePct * 100),
    trail_tight_bps: Math.round(s.trailTightPct * 100),
    ext_pct_threshold: s.extPctThreshold,
    slope_pct_threshold: s.slopePctThreshold,
    off_hod_pct_threshold: s.offHodPctThreshold,
    lower_high_bars: s.lowerHighBars,
    trim_max: s.trimMax,
    trim_fraction: s.trimFraction,
  };
}

/** The schema defaults, for the window before the settings call resolves. */
export function peakDefaults(): PeakDefaults {
  return peakParamsFrom(PEAK_SETTINGS_FALLBACK);
}

/**
 * Live PEAK params from the settings domain.
 *
 * `ready` is false until the real values arrive. The toggle uses it to stay disabled rather than arm a
 * position with fallback numbers the operator may have deliberately changed — arming is the gate, so the
 * gate has to be honest about what it is arming with.
 */
export function usePeakParams(): { params: PeakDefaults; ready: boolean; error: string | null } {
  const { data, isSuccess, isError, error } = useQuery({
    queryKey: ["settings", "peak"],
    queryFn: () => getSettings("peak"),
    staleTime: 60_000,
  });
  const values = data?.values as Partial<PeakSettings> | undefined;
  return {
    params: peakParamsFrom({ ...PEAK_SETTINGS_FALLBACK, ...(values ?? {}) }),
    ready: isSuccess,
    // A failed settings call otherwise disables the toggle with no explanation — the same "looks like a
    // dead button" failure #256 was about, reintroduced one layer up. react-query retries on its own, so
    // this is a reason shown, not a dead end. (codex review, Medium.)
    error: isError ? `PEAK settings unavailable — ${String(error)}` : null,
  };
}
