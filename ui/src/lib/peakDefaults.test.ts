import { describe, it, expect } from "vitest";
import {
  PEAK_SETTINGS_FALLBACK,
  peakDefaults,
  peakParamsFrom,
  type PeakSettings,
} from "./peakDefaults";

describe("peakParamsFrom", () => {
  it("converts trail widths from percent to basis points", () => {
    // The one real hazard in this file. Settings are in percent because that is what the operator reads;
    // the engine takes basis points because that is what the Nautilus trailing offset carries. Getting
    // this backwards would arm a 2.5 BASIS POINT trail — a stop 0.025% away, which exits on noise.
    const p = peakParamsFrom({ ...PEAK_SETTINGS_FALLBACK, trailWidePct: 2.5, trailTightPct: 1.0 });
    expect(p.trail_wide_bps).toBe(250);
    expect(p.trail_tight_bps).toBe(100);
  });

  it("leaves the signal thresholds in percent, because the engine reads them as percent", () => {
    const p = peakParamsFrom({
      ...PEAK_SETTINGS_FALLBACK,
      extPctThreshold: 40,
      slopePctThreshold: 3,
      offHodPctThreshold: 2,
    });
    expect(p.ext_pct_threshold).toBe(40);
    expect(p.slope_pct_threshold).toBe(3);
    expect(p.off_hod_pct_threshold).toBe(2);
  });

  it("rounds fractional percents to whole basis points", () => {
    expect(peakParamsFrom({ ...PEAK_SETTINGS_FALLBACK, trailWidePct: 1.234 }).trail_wide_bps).toBe(123);
  });

  it("keeps Alpaca's 0.1% floor expressible", () => {
    // Measured 2026-08-12: `Trail_percent must be >= 0.1`. The schema pins the same minimum, so the
    // smallest legal setting must survive the conversion as a usable value rather than round to zero.
    expect(peakParamsFrom({ ...PEAK_SETTINGS_FALLBACK, trailTightPct: 0.1 }).trail_tight_bps).toBe(10);
  });

  it("passes the counts through untouched", () => {
    const p = peakParamsFrom({ ...PEAK_SETTINGS_FALLBACK, lowerHighBars: 4, trimMax: 0, trimFraction: 0.3 });
    expect(p.lower_high_bars).toBe(4);
    expect(p.trim_max).toBe(0); // 0 = trimming off, trail does all the work
    expect(p.trim_fraction).toBe(0.3);
  });
});

describe("the fallback", () => {
  it("matches the schema's own defaults", () => {
    // If these drift, the schema wins at runtime and this only covers the window before the first
    // settings response — but a silent mismatch would mean the toggle briefly offers different numbers
    // from the ones the Settings tab shows.
    const expected: PeakSettings = {
      trailWidePct: 2.5,
      trailTightPct: 1.0,
      extPctThreshold: 40,
      slopePctThreshold: 3,
      offHodPctThreshold: 2,
      lowerHighBars: 3,
      trimMax: 2,
      trimFraction: 0.45,
    };
    expect(PEAK_SETTINGS_FALLBACK).toEqual(expected);
  });

  it("produces the params the engine validated against before settings existed", () => {
    expect(peakDefaults()).toEqual({
      trail_wide_bps: 250,
      trail_tight_bps: 100,
      ext_pct_threshold: 40,
      slope_pct_threshold: 3,
      off_hod_pct_threshold: 2,
      lower_high_bars: 3,
      trim_max: 2,
      trim_fraction: 0.45,
    });
  });

  it("stays inside the engine's own range checks", () => {
    // `_validate_peak_ranges` refuses tight >= wide, trim_fraction outside (0,1], lower_high_bars < 2.
    // A default that could not arm would be a strange thing to ship.
    const p = peakDefaults();
    expect(p.trail_tight_bps).toBeLessThan(p.trail_wide_bps);
    expect(p.trim_fraction).toBeGreaterThan(0);
    expect(p.trim_fraction).toBeLessThanOrEqual(1);
    expect(p.lower_high_bars).toBeGreaterThanOrEqual(2);
  });
});
