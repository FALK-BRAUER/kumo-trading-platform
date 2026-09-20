/**
 * What the UNCLAIMED row may claim about the broker (#807 item 3).
 *
 * `venue_qty` is the broker's quantity for the symbol, published by the engine off the same snapshot
 * that feeds the drift banner. THREE STATES, deliberately: `0` means the broker answered and holds
 * NONE — the row is a phantom (a reconciliation-minted counterparty), not a holding, and "tap to move
 * to a strategy" would claim shares that do not exist; `null`/`undefined` means the broker has not
 * answered yet, which is not the same thing and must not read as either.
 */
export type PhantomState = "phantom" | "backed" | "unconfirmed";

export function phantomState(venueQty: number | null | undefined): PhantomState {
  if (venueQty === null || venueQty === undefined) return "unconfirmed";
  return venueQty === 0 ? "phantom" : "backed";
}

/** The sub-line's origin clause. ONLY a backed row keeps the move affordance: a phantom has nothing
 *  to move, and an unconfirmed row has not been told — absence is not permission (codex review). */
export function phantomLabel(state: PhantomState, originLabel: string): string {
  if (state === "phantom") return "phantom — broker holds 0, cannot be moved";
  if (state === "unconfirmed") return `${originLabel} — broker unconfirmed, cannot be moved yet`;
  return `${originLabel} — tap to move to a strategy`;
}

/** Whether the row may open the move-to-strategy detail. One derivation for the label and the click. */
export function movable(state: PhantomState): boolean {
  return state === "backed";
}
