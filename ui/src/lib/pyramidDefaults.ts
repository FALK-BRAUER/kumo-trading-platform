/**
 * PYRAMID (#38) toggle defaults — mirrors `lib/peakDefaults.ts`'s split (pure logic, no JSX, importable by a
 * node-env test without pulling in PositionDetail.tsx transitively).
 *
 * `driver_instrument_id` is NOT here — unlike every other param, it's not a fixed threshold, it's a symbol
 * The operator types per-position (no auto sector/driver mapping exists — #38 scoping, this session: manual at arm
 * time). `initial_qty`/`rung_count`/`current_trail_coid`/`initial_risk_per_share` are also NOT sent by the
 * toggle — the engine computes/sets them itself when arming (`_handle_attach_manager_command`'s
 * `pyramid_watch` branch), since only the engine can read the position's existing bracket stop to derive R.
 */
export interface PyramidDefaults {
  add_r_multiple: number;
  max_rungs: number;
  trail_bps: number;
  daily_lookback_days: number;
  min_rel_volume: number;
  sustained_bars: number;
  off_hod_pct_threshold: number;
  lower_high_bars: number;
}

export function pyramidDefaults(): PyramidDefaults {
  return {
    add_r_multiple: 0.75, // fraction of the original R sized per tranche — matches the design mock's "+0.75R"
    max_rungs: 3, // budget cap — matches the design mock's "max 3 rungs"
    trail_bps: 150, // 1.5% — width the stop is raised to on each add
    daily_lookback_days: 20, // prior-range window the base-break proxy checks against
    min_rel_volume: 1.5, // relative-volume threshold that confirms a real breakout, not a thin poke
    sustained_bars: 2, // consecutive 1m bars the volume confirmation must hold, anti-noise (same discipline PEAK's fade-confirmation uses)
    off_hod_pct_threshold: 2, // sustained %-off-HoD that confirms a fade (own position or driver)
    lower_high_bars: 3, // consecutive lower-high bars that also confirms a fade
  };
}
