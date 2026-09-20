/** Per-viewer persistence for the `$` / `%` toggle (#586).
 *
 * Separate from `unit.ts` so the arithmetic module stays pure and importable anywhere, including from
 * tests that must not touch `window`. Modelled on `tiles/watchlist/persistence.ts`, which is the
 * repo's existing localStorage pattern.
 *
 * NEVER THROWS. A cockpit that fails to render because a preference could not be read is a far worse
 * outcome than a preference that silently reverts to the default — localStorage is absent under SSR
 * and during the Next build, and it throws outright in a private window or when site data is blocked.
 * Both directions are guarded and both fall back to `$`, which is what every figure meant before this
 * toggle existed.
 */
export type { Unit } from "./unit";
import type { Unit } from "./unit";

const KEY = "kumo.unit";

export function readUnit(): Unit {
  if (typeof window === "undefined") return "$"; // SSR / build — no localStorage
  try {
    // VALIDATED, never trusted raw: a hand-edited value or a prior schema version must not put the
    // panel into a state it has no rendering for. Same rule as the watchlist config reader.
    return window.localStorage.getItem(KEY) === "%" ? "%" : "$";
  } catch {
    return "$";
  }
}

export function writeUnit(unit: Unit): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(KEY, unit);
  } catch {
    // A viewer who blocks site data still gets the toggle for this session; it simply will not
    // survive a reload. Losing the preference is not worth losing the interaction.
  }
}
