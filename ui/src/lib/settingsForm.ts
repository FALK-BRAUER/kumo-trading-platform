/**
 * Settings-form helpers (#49) — pure, node-testable. Kept out of the React component so the save-payload
 * rule (which the backend validates strictly) has a unit test.
 */
import type { SettingsValues } from "@/lib/api/client";

/** Drop cleared fields (null / undefined / "") from a save payload so the backend fills the schema default
 *  instead of rejecting an empty value — e.g. clearing a numeric input must reset to default, not 422. */
export function stripEmpty(values: SettingsValues): SettingsValues {
  const out: SettingsValues = {};
  for (const [k, v] of Object.entries(values)) {
    if (v !== null && v !== undefined && v !== "") out[k] = v;
  }
  return out;
}
