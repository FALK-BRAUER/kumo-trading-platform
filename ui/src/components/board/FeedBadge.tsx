"use client";

/**
 * FEED — is data arriving right now (#384 step 1).
 *
 * The operator asked for "a label that says live if live data is feeding". It is NOT called LIVE: that word is
 * already the environment badge two elements to the right, where it means orders go to a real-money
 * account. Two badges reading LIVE, one meaning "real money" and one meaning "ticks arriving", is a
 * collision where the reassuring reading of one is the dangerous reading of the other.
 *
 * WHY A CHIP AND NOT A BANNER. The banner already fires when the feed is DOWN. This answers the quieter
 * question that produced two incidents in one morning — "is what I am looking at current?" — for which
 * the answer was previously discoverable only by asking someone. It is deliberately small: a chip you
 * can ignore when it is green is the point.
 *
 * The rule lives in `feedFreshness` and is shared with `health.ts`, so the chip and the banner cannot
 * disagree about whether the feed is alive.
 */
import { useEffect, useState } from "react";
import { useHealth } from "@/lib/framework/health";

export function FeedBadge() {
  // Its own tick, because freshness is a function of TIME as much as of data: a feed that stopped
  // produces no re-render, which is exactly when the chip most needs to change.
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 10_000);
    return () => clearInterval(id);
  }, []);

  // The SAME derivation the banner gates on — returned by `useHealth`, not recomputed here. Two answers
  // to "is the feed alive" is the failure this codebase keeps producing; one poll, one rule.
  const { feed } = useHealth(nowMs);

  const cls =
    feed.tone === "live"
      ? "bg-status-bull/10 text-status-bull ring-status-bull/30"
      : feed.tone === "lagging"
        ? "bg-status-watch/15 text-status-watch ring-status-watch/40"
        : feed.tone === "down"
          ? "bg-status-bear/15 text-status-bear ring-status-bear/40"
          : "bg-ds-line/40 text-t3 ring-ds-line";

  return (
    <span
      className={`shrink-0 whitespace-nowrap rounded px-2 py-0.5 font-mono text-[10px] ring-1 ${cls}`}
      title={
        feed.ageSec == null
          ? "No market-data tick has been seen at all."
          : `Last market-data tick ${Math.round(feed.ageSec)}s ago. Outside market hours a quiet feed is normal and this reads 'idle' rather than alarming.`
      }
    >
      {feed.tone === "live" ? "● " : ""}
      {feed.label}
    </span>
  );
}
