/**
 * toggleStatus (#256) — turn a manager toggle's command ack into the line the operator reads.
 *
 * Every manager toggle (STOP-AND-REENTER, PEAK, PYRAMID) polled its ack for two things only: the pending
 * spinner, and invalidating queries on `accepted`. A **rejected** ack was dropped. The whole visible
 * sequence of a refused arm was therefore: click ON, brief spinner, toggle is OFF again — no message, no
 * reason, indistinguishable from a dead button.
 *
 * That hid PYRAMID entirely. It refuses to arm without a bracket-tagged protective stop (it needs one to
 * compute R), and the book has had none since #239 — so every arm was rejected, with a perfectly good
 * explanation from the engine that nobody ever saw. `pyramid_watch` has zero rows in the manager table:
 * it has never armed once since it was built (#257).
 *
 * A refusal is information. `null` here means "nothing to say" — not "it worked".
 */
import type { CommandState } from "@/lib/api/client";

export interface ToggleAck {
  state: CommandState;
  error: string | null;
}

/**
 * Durable truth, from the manager list rather than the command ack (2026-08-21).
 *
 * `useCommandStatus` gives up after its timeout and resolves to `unknown`, which is deliberately NOT
 * a failure — the command may well have applied. PEAK's toggle passes no timeout, so it uses the 8s
 * default where flatten passes 30s; `/managers` refetches on a 15s interval. A successful arm therefore
 * rendered as "no answer from the engine" for up to ~23 seconds. Measured on the live book: peak_watch
 * on MRVL.XNAS went ARMED -> APPLIED 29ms apart, and the operator still saw a timeout.
 *
 * The screen already knows the answer — `activePeak` is derived from the same manager list. This lets
 * the toggle DEFER to it. It can only ever RESOLVE an `unknown`; it can never overrule a `rejected`,
 * because a refusal is information and a stale row from a prior cycle must not be able to erase one
 * (#256/#257 — PYRAMID's rejections were invisible for its entire life).
 */
export interface ToggleDurable {
  durablyOn: boolean;
}

type ToggleMutation = { isError?: boolean; error?: unknown; isPending?: boolean };

/** Varargs carry mutations; an object with `durablyOn` is the durable-state opt-in, not a mutation.
 *  Split explicitly — passing it through as a mutation is exactly the bug the tests caught first. */
function splitDurable(
  rest: Array<ToggleMutation | ToggleDurable>,
): { mutations: ToggleMutation[]; durablyOn: boolean } {
  const mutations: ToggleMutation[] = [];
  let durablyOn = false;
  for (const r of rest) {
    if (r && typeof r === "object" && "durablyOn" in r) durablyOn = Boolean(r.durablyOn);
    else mutations.push(r as ToggleMutation);
  }
  return { mutations, durablyOn };
}

/**
 * The operator-facing reason a toggle did not take, or `null` when there is nothing to report.
 *
 * Ordered by what the operator can act on. A transport failure means the command never reached the
 * engine, which is a different problem from the engine refusing it, which is different again from an ack
 * that never came back — in the last case the command may well have applied, so the copy sends them to
 * the blotter rather than implying failure (the same rule `useCommandStatus` follows when it resolves a
 * lost ack to `unknown` instead of a false `rejected`).
 */
export function toggleError(
  ack: ToggleAck,
  ...rest: Array<{ isError: boolean; error: unknown } | ToggleDurable>
): string | null {
  const { mutations, durablyOn } = splitDurable(rest as Array<ToggleMutation | ToggleDurable>);
  // Every mutation behind the toggle, not a hand-picked one. The first version took a single mutation
  // chosen by `attach.isError ? attach : cancel`, which let a stale failure from the OTHER operation win:
  // react-query clears a mutation's error only when that same mutation reruns, so a failed arm kept
  // rendering over a later successful cancel — and could mask a fresh rejection. Both are `.reset()` on
  // every toggle press now, so at most the current attempt can be in error here. (codex review, High.)
  const failed = mutations.find((m) => m.isError);
  if (failed) return `could not reach the engine — ${String(failed.error)}`;
  if (ack.state === "rejected") return ack.error ?? "the engine refused this";
  // A LOST ACK DEFERS TO DURABLE STATE. `unknown` means "we stopped waiting", not "it failed"; when the
  // manager list already shows this armed, the engine plainly did answer and saying otherwise sends the
  // operator to the blotter to confirm something the same screen is already displaying.
  if (ack.state === "unknown") {
    return durablyOn ? null : "no answer from the engine — check the blotter before retrying";
  }
  return null;
}

/**
 * Whether a toggle is still locked. Extracted alongside `toggleError` because the two were derived
 * inconsistently at each call site.
 *
 * `unknown` counts as locked, not as finished (codex review, round 2). `useCommandStatus` resolves to
 * `unknown` on its OWN 8-second timeout, which says nothing about the engine — the command may still be
 * queued, and may yet be accepted. Re-enabling the toggle there invites a second arm for a command that
 * already worked, which is precisely how FIG collected duplicate managers on 2026-08-11 when the toggles
 * lagged and were clicked repeatedly.
 *
 * So after a lost ack the toggle stays locked and `toggleError` says to check the blotter. The exit is in
 * `PositionDetail`: when the manager list refetches and the active manager for that toggle changes, the
 * command id is cleared and the lock lifts, because the durable row has answered the question the ack
 * could not. Without that the lock would be permanent for as long as the pane stayed open — an earlier
 * draft of this comment claimed it resolved itself, and it did not (codex review, round 3).
 *
 * Erring toward locked is deliberate: a toggle that refuses a second press costs a wait, one that accepts
 * it costs a duplicate manager on a live position.
 */
export function togglePending(
  commandId: string | null,
  ack: ToggleAck,
  ...rest: Array<{ isPending: boolean } | ToggleDurable>
): boolean {
  const { mutations, durablyOn } = splitDurable(rest as Array<ToggleMutation | ToggleDurable>);
  if (mutations.some((m) => m.isPending)) return true;
  if (commandId === null) return false;
  // `pending` stays locked even when durably on — the command has not resolved, and unlocking there
  // would let a second press land while the first is still in flight.
  if (ack.state === "pending") return true;
  // `unknown` is the stuck-spinner case: we gave up waiting, the manager list says it armed, so the
  // lock is no longer protecting anything.
  return ack.state === "unknown" && !durablyOn;
}
