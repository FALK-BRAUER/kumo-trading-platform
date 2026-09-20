import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { feedFreshness } from "./feedFreshness";

const NOW = Date.parse("2026-08-20T12:01:00Z");
const ns = (secAgo: number) => (NOW - secAgo * 1000) * 1e6;

describe("the feed says whether it is feeding (#384)", () => {
  it("is live while ticks are arriving", () => {
    // Measured on the running stack: feed_last_tick_ts was 2s old through the whole pre-market.
    expect(feedFreshness(ns(2), NOW, "PRE").tone).toBe("live");
    expect(feedFreshness(ns(2), NOW, "PRE").label).toBe("feed live");
  });

  it("degrades before it alarms", () => {
    expect(feedFreshness(ns(90), NOW, "OPEN").tone).toBe("lagging");
    expect(feedFreshness(ns(600), NOW, "OPEN").tone).toBe("down");
    expect(feedFreshness(ns(600), NOW, "OPEN").label).toBe("feed 10m");
  });

  it("does not cry wolf when the market is shut", () => {
    // A quiet feed at 03:00 is not a fault, and an indicator that alarms nightly is one you stop
    // reading — the same reasoning that made SECURED state its own zero rather than show a bare $0.00.
    expect(feedFreshness(ns(40_000), NOW, "CLOSED").tone).toBe("idle");
    expect(feedFreshness(null, NOW, "CLOSED").tone).toBe("idle");
  });

  it("treats a never-ticked feed inside a session as down, not idle", () => {
    // This is the alarm's whole reason to exist: the engine up, the tiles rendering, and nothing
    // arriving. #363 and #368 are both that shape — RUNNING and publishing nothing.
    const f = feedFreshness(null, NOW, "OPEN");
    expect(f.tone).toBe("down");
    expect(f.label).toBe("no feed");
    expect(f.ageSec).toBeNull();
  });

  it("reads ages in units a person can act on", () => {
    expect(feedFreshness(ns(45), NOW, "OPEN").label).toBe("feed live");
    expect(feedFreshness(ns(120), NOW, "OPEN").label).toBe("feed 2m");
    expect(feedFreshness(ns(7200), NOW, "OPEN").label).toBe("feed 2h");
  });

  it("never renders a future tick as a huge age", () => {
    expect(feedFreshness(ns(-30), NOW, "OPEN").ageSec).toBe(0);
    expect(feedFreshness(ns(-30), NOW, "OPEN").tone).toBe("live");
  });
});

describe("one derivation, two consumers", () => {
  it("health.ts gates the banner on feedFreshness rather than its own tick maths", () => {
    // It used to compute `tickAge > TICK_STALE_MS` itself while the header chip computed another — two
    // answers to "is the feed alive", which is the failure mode this repo has measured repeatedly. The
    // chip now reads what `useHealth` returns; nothing recomputes it.
    const src = readFileSync(join(import.meta.dirname, "framework", "health.ts"), "utf8");
    const code = src.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
    expect(code).toMatch(/feedFreshness\(/);
    expect(code).not.toMatch(/TICK_STALE_MS/);
    expect(code).toMatch(/feed\b/);

    const badge = readFileSync(join(import.meta.dirname, "..", "components", "board", "FeedBadge.tsx"), "utf8");
    const bcode = badge.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
    expect(bcode).not.toMatch(/feedFreshness\(/); // reads the shared result, does not re-derive
  });

  it("is not called LIVE — that word already means real-money orders", () => {
    // `page.tsx`: "LIVE is red and unmissable" for the ENVIRONMENT badge. A second LIVE meaning "ticks
    // arriving" beside it is a collision where the reassuring reading of one is the dangerous reading
    // of the other.
    const src = readFileSync(join(import.meta.dirname, "feedFreshness.ts"), "utf8");
    const code = src.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
    expect(code).not.toMatch(/"LIVE"|'LIVE'|`LIVE`/);
    expect(code).toMatch(/feed live/);
  });
})

/**
 * IB DELAYED market data is ~15 minutes behind BY DESIGN (#834). Measured on ibkr-paper 2026-09-09:
 * the real-time stream is refused silently (one market-data session per user), DELAYED is what IB
 * serves without it, and under DELAYED a perfectly healthy feed presents as ~900s old — which the
 * thresholds above call "down". The print type has to travel with the stamp.
 */
describe("a DELAYED feed is judged against its own lag and SAYS it is delayed (#834)", () => {
  const openSession = "OPEN" as const;
  const ns = (secAgo: number) => (NOW - secAgo * 1000) * 1e6;

  it("fixture: 900s old under REALTIME is DOWN — otherwise the DELAYED case below cannot fail", () => {
    expect(feedFreshness(ns(900), NOW, openSession, "REALTIME").tone).toBe("down");
    expect(feedFreshness(ns(900), NOW, openSession, null).tone).toBe("down");
  });

  it("900s old under DELAYED is live, and the label says so", () => {
    const f = feedFreshness(ns(900), NOW, openSession, "DELAYED");
    expect(f.tone).toBe("live");
    expect(f.label.toLowerCase()).toContain("delayed");
    expect(f.label.toLowerCase()).not.toContain("feed live");
  });

  it("the raw age is reported, not the excused one", () => {
    // A consumer saying "last tick N s ago" must show the true number; the excuse is in the tone.
    expect(feedFreshness(ns(900), NOW, openSession, "DELAYED").ageSec).toBeCloseTo(900, 0);
  });

  it("DELAYED still degrades and alarms once it falls behind its OWN lag", () => {
    expect(feedFreshness(ns(900 + 120), NOW, openSession, "DELAYED").tone).toBe("lagging");
    expect(feedFreshness(ns(900 + 400), NOW, openSession, "DELAYED").tone).toBe("down");
  });

  it("outside a session DELAYED is idle like everything else", () => {
    expect(feedFreshness(ns(900), NOW, "CLOSED", "DELAYED").tone).toBe("idle");
  });
})
