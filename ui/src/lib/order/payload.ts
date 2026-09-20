/**
 * Order-core payload builders (#66) — PURE functions that turn the ticket's field state into the exact
 * API payloads (`OrderPayload` / `BracketPayload`). No React, no transport → unit-testable, and SHARED by
 * every order-tile variant (vanilla / assisted) + the modal, so the wire shape has one definition.
 *
 * These encode the venue coercions (extended-hours → DAY limit at Alpaca; stop-entry uses the price field
 * as its trigger; a bracket entry is market|limit only) in ONE place instead of per component.
 */
import type { BracketPayload, OrderPayload } from "@/lib/api/client";

export type Action = "BUY" | "SELL";
export type OrderType = "market" | "limit" | "stop";

/** The subset of ticket state a single (non-bracket) order needs. */
export interface OrderTicketState {
  instrumentId: string;
  action: Action;
  orderType: OrderType;
  extended: boolean; // pre/post-market — Alpaca requires a DAY limit order
  /** The price field: the limit price for a limit order, or the trigger for a stop order. */
  priceNum: number;
  tif: string; // day | gtc | opg | cls
  sharesNum: number;
}

/** The subset a bracketed entry needs (entry + protective stop + target). Entry is market|limit ONLY — a
 *  stop entry is N/A for brackets, so the type forbids it (don't silently coerce a bug into a market order). */
export interface BracketTicketState {
  instrumentId: string;
  action: Action;
  orderType: "market" | "limit";
  priceNum: number; // entry limit price (limit entry)
  sharesNum: number;
  stopNum: number; // protective stop trigger
  targetNum: number; // take-profit
  tif: string;
}

/** Build the single-order payload, applying the extended-hours → DAY-limit coercion (defense-in-depth over
 *  the UI constraints) and routing the price field to `price` (limit) or `trigger_price` (stop). */
export function buildOrderPayload(s: OrderTicketState): OrderPayload {
  const effectiveType: OrderType = s.extended ? "limit" : s.orderType;
  return {
    instrument_id: s.instrumentId,
    side: s.action,
    quantity: s.sharesNum,
    order_type: effectiveType,
    price: effectiveType === "limit" ? s.priceNum : undefined,
    trigger_price: effectiveType === "stop" ? s.priceNum : undefined,
    time_in_force: s.extended ? "day" : s.tif, // Alpaca: extended-hours must be DAY
    extended_hours: s.extended,
  };
}

/** Build the bracket payload (entry + protective stop + target). Entry is market|limit; `price` only rides
 *  a limit entry. */
export function buildBracketPayload(s: BracketTicketState): BracketPayload {
  return {
    instrument_id: s.instrumentId,
    side: s.action,
    quantity: s.sharesNum,
    entry_order_type: s.orderType,
    price: s.orderType === "limit" ? s.priceNum : undefined,
    stop_trigger: s.stopNum,
    target_price: s.targetNum,
    time_in_force: s.tif,
  };
}

/** A stop-ORDER trigger must sit on the correct side of the reference price — BUY stops trigger ABOVE, SELL
 *  stops BELOW — else it fires instantly as a market order. Non-stop orders always pass. */
export function stopTriggerOk(action: Action, orderType: OrderType, triggerNum: number, ref: number): boolean {
  if (orderType !== "stop") return true;
  if (!(triggerNum > 0) || !(ref > 0)) return false;
  return action === "BUY" ? triggerNum > ref : triggerNum < ref;
}

/**
 * Time-in-force for a BRACKET, which is not a free choice: Alpaca applies the parent's `time_in_force` to
 * both child legs, and its bracket API has no per-leg field (`stop_loss` / `take_profit` take prices only).
 * So the entry's TIF decides how long the PROTECTIVE STOP lives.
 *
 * Measured on a live instance paper book, 2026-08-15: every stop the #239 backstop placed rested `gtc`, and the
 * single `day` protection was a dialogue bracket — whose stop therefore expires at 16:00 and leaves the
 * position naked until the backstop's first regular-hours tick. That overnight window is the exact gap
 * #239 exists to close, reopened by the ticket. This was hardcoded `"day"` at the call site (#315).
 *
 * MARKET entry -> `gtc`. The entry normally fills on submission, so its own TIF rarely matters, and the
 * legs inherit protection that outlives the session. Not entirely free, and the earlier note here saying
 * "strictly better, no trade-off" overclaimed (codex review): a market parent that is ACCEPTED but not
 * filled — a halt, or a venue that queues it — stays alive under `gtc` where `day` would have killed it
 * at the close. The ticket blocks submission when a market order would be cancelled outright, so the
 * exposure is narrow, but it is not zero.
 *
 * LIMIT entry -> `day`. `gtc` would buy protection by leaving an unfilled ENTRY resting indefinitely, and
 * a buy order that can fill days later on a name the operator has stopped watching is a worse hazard than
 * a stop that expires with the backstop standing behind it. The caller must SAY so rather than let the
 * operator assume the stop is permanent — see `bracketProtectionExpires`.
 */
export function bracketTif(orderType: "market" | "limit"): "gtc" | "day" {
  return orderType === "market" ? "gtc" : "day";
}

/**
 * Does a bracket resting on this time-in-force lose its protective stop at the close?
 *
 * Takes the TIF rather than the order type because the two tickets reach it differently — one derives it
 * (`bracketTif`), one lets the operator pick — and both must ask the SAME question, or the two screens
 * drift into disagreeing about when protection expires. One predicate, two callers.
 */
export function protectionExpiresForTif(tif: string): boolean {
  return tif !== "gtc";
}

/** Convenience for the ticket that derives its TIF rather than offering a control. */
export function bracketProtectionExpires(orderType: "market" | "limit"): boolean {
  return protectionExpiresForTif(bracketTif(orderType));
}

/** Bracket geometry: a BUY needs stop < entry < target; a SELL needs target < entry < stop. Any non-positive
 *  leg fails (an incomplete bracket is not submittable). */
export function bracketGeoOk(action: Action, entry: number, stop: number, target: number): boolean {
  if (!(entry > 0) || !(stop > 0) || !(target > 0)) return false;
  return action === "BUY" ? stop < entry && entry < target : target < entry && entry < stop;
}
