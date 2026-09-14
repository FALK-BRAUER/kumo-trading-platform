/**
 * P&L tone (#104 / #114) — the semantic-token text class for a signed value. Positive = bull, negative
 * = bear, flat/unknown = muted grey. Row/format contract: P&L tints the number (colour + sign), never a
 * whole row. JSX-free so it unit-tests under the node-env vitest config; used by tiles and detail screens.
 */
export function pnlToneClass(value: number | null | undefined): string {
  if (value == null || value === 0) return "text-t2";
  return value > 0 ? "text-status-bull" : "text-status-bear";
}
