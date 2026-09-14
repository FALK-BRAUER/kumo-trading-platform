/**
 * Is data actually feeding right now? (#384 step 1)
 *
 * Operator: "a label that says 'live' if live data is feeding". This is that — under a different word, for a
 * reason that matters.
 *
 * IT IS NOT CALLED "LIVE". `LIVE` is already the ENVIRONMENT badge in the header, and it means the
 * orders go to a real-money account — "LIVE is red and unmissable" (page.tsx). A second LIVE meaning
 * "the ticks are arriving" beside it is the worst kind of collision: the safe reading of one is the
 * dangerous reading of the other. This says FEED.
 *
 * WHY IT EARNS ITS SPACE. Two incidents on 2026-08-20 were both "is what I am looking at current?" —
 * a rotation read 18 hours old rendering exactly like a fresh one, and a premarket price that turned
 * out to be an offer. A feed indicator answers that class directly, once, instead of per tile.
 *
 * SESSION-AWARE, because a quiet feed at 03:00 is not a fault. Alarming outside market hours is how an
 * indicator teaches you to ignore it — the same reasoning that made SECURED state its own zero.
 */
import type { MarketSession } from "./market";

export type FeedTone = "live" | "lagging" | "down" | "idle";

export interface FeedFreshness {
  tone: FeedTone;
  /** Short enough for a header chip. */
  label: string;
  ageSec: number | null;
}

const LAGGING_S = 60;
const DOWN_S = 300;

/**
 * IB's DELAYED market data runs ~15 minutes behind BY DESIGN (#834). A delayed feed that is working
 * perfectly therefore presents as 900s old, which the thresholds below would call "down". The lag is
 * subtracted from the age before judging freshness, and the label says DELAYED so nobody reads a
 * quarter-hour-old print as the current one — that is the entry-price hazard, and it is the whole
 * reason the print type travels with the stamp instead of being inferred from the age.
 */
const DELAYED_LAG_S = 15 * 60;

/**
 * `tickTsNs` is the engine's `feed_last_tick_ts` — epoch NANOseconds, or 0/null when it never ticked.
 * `marketDataType` is the print type behind that stamp ("REALTIME" / "DELAYED" / null); only DELAYED
 * changes the judgement.
 */
export function feedFreshness(
  tickTsNs: number | null | undefined,
  nowMs: number,
  session: MarketSession,
  marketDataType?: string | null,
): FeedFreshness {
  const ns = typeof tickTsNs === "number" && Number.isFinite(tickTsNs) && tickTsNs > 0 ? tickTsNs : null;
  if (ns === null) {
    // Never ticked. Outside a session that is unremarkable; inside one it is the whole alarm.
    return session === "CLOSED"
      ? { tone: "idle", label: "feed idle", ageSec: null }
      : { tone: "down", label: "no feed", ageSec: null };
  }
  const delayed = marketDataType === "DELAYED";
  const ageSec = Math.max(0, (nowMs - ns / 1e6) / 1000);
  // Judge the feed on how far BEHIND ITS OWN EXPECTED LAG it is. `ageSec` stays the raw age — a
  // consumer showing "last tick N s ago" must show the true number, not the excused one.
  const excess = delayed ? Math.max(0, ageSec - DELAYED_LAG_S) : ageSec;
  const tag = delayed ? "delayed " : "";
  if (session === "CLOSED") return { tone: "idle", label: "feed idle", ageSec };
  if (excess <= LAGGING_S) return { tone: "live", label: delayed ? "feed delayed ~15m" : "feed live", ageSec };
  if (excess <= DOWN_S) return { tone: "lagging", label: `feed ${tag}${humanAge(ageSec)}`, ageSec };
  return { tone: "down", label: `feed ${tag}${humanAge(ageSec)}`, ageSec };
}

function humanAge(sec: number): string {
  if (sec < 90) return `${Math.round(sec)}s`;
  if (sec < 5400) return `${Math.round(sec / 60)}m`;
  return `${Math.round(sec / 3600)}h`;
}
