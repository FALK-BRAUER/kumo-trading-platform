/**
 * How a data frame's own reported health becomes a tile state (#298).
 *
 * On 2026-08-14 a NameError in the engine's trade-cycle projection made every tick raise. The engine
 * survived and logged — both correct — but published nothing, so the book tile rendered EMPTY while eight
 * positions worth $68k were held at the broker, and said nothing was wrong.
 *
 * An empty book is indistinguishable from a LIQUIDATED book, and those two demand opposite reactions from
 * whoever is looking. There is also a legitimate empty window at startup while reconciliation runs. Three
 * states, one rendering — so the display was confidently wrong at the moment it mattered most.
 *
 * The engine now reports which kind of empty it is. This turns that into something a tile can render.
 */

/** What the engine says about the frame it just published. */
export type FeedStatus = "ok" | "seeding" | "failed";

export interface FeedState {
  /** `true` when the rows are real and current. */
  healthy: boolean;
  /** Shown instead of content when there is nothing trustworthy to show. */
  blockingMessage: string | null;
  /** Shown ABOVE content that is real but no longer updating. */
  banner: string | null;
}

interface FrameLike {
  status?: string;
  error?: string | null;
}

/**
 * `frame` is whatever the engine published. A frame with no `status` at all is treated as healthy —
 * older engines do not send one, and refusing to render against an engine that predates this field would
 * be a worse failure than the one it fixes.
 */
export function feedState(frame: FrameLike | undefined, hasRows: boolean): FeedState {
  const status = frame?.status as FeedStatus | undefined;

  if (status === "seeding") {
    // Legitimate. `held: 0` right after a restart is reconciliation lag, not liquidation — a rule that
    // used to live in a handoff document rather than on the screen.
    return {
      healthy: false,
      blockingMessage: "Reconciling with the broker…",
      banner: null,
    };
  }

  if (status === "failed") {
    // Real rows from before the failure are worth MORE than a blank tile: they preserve the operator's
    // mental model instead of destroying it. Blanking is what made this alarming. But they must never be
    // presented as current.
    return hasRows
      ? { healthy: false, blockingMessage: null, banner: bannerText(frame?.error) }
      : { healthy: false, blockingMessage: `Position data unavailable — ${reason(frame?.error)}`, banner: null };
  }

  return { healthy: true, blockingMessage: null, banner: null };
}

function reason(error: string | null | undefined): string {
  const text = (error ?? "").trim();
  return text.length > 0 ? text : "the engine could not build it";
}

function bannerText(error: string | null | undefined): string {
  return `NOT UPDATING — last known values. ${reason(error)}`;
}
