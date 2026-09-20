/**
 * Order-ticket mode exclusion (#43) — EXT-HRS, BRACKET, and AUTO are mutually exclusive: at most one is
 * on at a time. This is the ONE place the invariant lives, as a pure function so it's node-testable
 * (a one-directional exclusion bug — AUTO overwriting an extended order's type — got past tsc + the pure
 * libs; this locks it). The ticket's toggle handlers call this, then apply their own type side-effects.
 */
export interface OrderModes {
  extended: boolean;
  bracket: boolean;
  autoSel: boolean;
}

export type ModeName = keyof OrderModes;

/** Toggle one mode. Turning a mode ON turns the other two OFF; turning it OFF clears all (only one is ever
 *  on, so "off" is all-off — returning all-false is self-defending even against a hypothetical two-on input). */
export function toggleMode(cur: OrderModes, which: ModeName): OrderModes {
  if (cur[which]) return { extended: false, bracket: false, autoSel: false };
  return { extended: which === "extended", bracket: which === "bracket", autoSel: which === "autoSel" };
}
