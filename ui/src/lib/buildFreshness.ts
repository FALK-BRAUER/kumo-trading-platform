/**
 * Is this browser running the build the server is currently serving? (#345 item 5)
 *
 * WHAT HAPPENED. A phone served a 10-minute-old bundle showing NET·all **+$847.61** when the deployed
 * answer was **+$250.64**. Wrong money, rendered indistinguishably from a live read. Same failure class
 * as #336/#343 — a plausible wrong number in the hero slot that nobody re-checks — except the defect is
 * in DELIVERY rather than derivation, so no amount of correctness in the engine can prevent it.
 *
 * The operator debugs from that phone, which makes it his primary read of the account.
 *
 * WHY THE API'S BUILD ID IS THE WRONG THING TO COMPARE AGAINST. `deploy/Makefile` computes the UI's
 * `BUILD_ID` from `git log -1 -- ui/ deploy/Dockerfile.ui` and the backend's `KUMO_GIT_SHA` from
 * `HEAD` — DIFFERENT revisions on purpose, because `deploy-ui-paper` ships the UI without touching the
 * engine. Comparing them would disagree on every correctly-deployed stack: an alarm that fires on
 * healthy state, which this codebase has shipped twice (#387, #390) and paid for both times.
 *
 * WHAT IS ACTUALLY COMPARED. The build id BAKED into the running bundle at compile time against the one
 * the UI SERVER reports at request time. Those are the same value on a fresh load and diverge for
 * exactly one reason: the browser is executing JavaScript from an earlier deploy. That is the failure,
 * stated precisely, with no second cause to confuse it with.
 */

export type BuildFreshness =
  | { kind: "fresh" }
  /** The running bundle predates what the server now serves — reload. */
  | { kind: "stale"; running: string; serving: string }
  /** One side could not be read. NOT stale: an unreachable check is not evidence of anything. */
  | { kind: "unknown"; reason: string };

export function buildFreshness(
  running: string | null | undefined,
  serving: string | null | undefined,
): BuildFreshness {
  const r = (running ?? "").trim();
  const s = (serving ?? "").trim();
  // UNKNOWN, NOT STALE. A dev build has no id, and a failed fetch has no answer. Reporting either as
  // "your page is out of date" is the cry-wolf direction, and a banner an operator learns to dismiss is
  // worse than no banner — it is the one that will be dismissed on the day it is right.
  if (!r) return { kind: "unknown", reason: "this bundle carries no build id" };
  if (!s) return { kind: "unknown", reason: "the server did not report a build id" };
  if (r === s) return { kind: "fresh" };
  return { kind: "stale", running: r, serving: s };
}
