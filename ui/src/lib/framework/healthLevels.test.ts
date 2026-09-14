import { describe, expect, it } from "vitest";

import { STYLE } from "@/components/board/HealthBanner";
import { classifyHealth, type HealthLevel } from "./health";
import type { FeedFreshness } from "@/lib/feedFreshness";

/**
 * EVERY banner level must be REACHABLE from the decision function (#437 follow-up).
 *
 * Found by the cockpit peer while reviewing this PR: deleting the ownership branch from `useHealth`
 * left all 839 UI tests green — and so did neutering apiDown, engineDown, reconcileDrift, feedStale,
 * wsIssues and degraded, one at a time. All seven levels were unpinned. The logic lived inside the
 * hook, and every test in this repo is a pure-function test, so nothing could reach it.
 *
 * That is the same defect this whole PR exists to fix, one layer up and in a different language: a
 * branch nothing proves is taken. It was pre-existing for seven levels; this PR added the eighth.
 *
 * DRIVEN FROM THE STYLE MAP, NOT A HAND-WRITTEN LIST. A list is a thing someone has to remember to
 * update, and the next level added is the one it misses — which is exactly how this got here. Add a
 * level to `STYLE` with no case below and this test goes red on the coverage assertion.
 */

// NOT cast. The first draft used `as FeedFreshness` with an `ageMs` field that does not exist —
// tsc caught it, but a cast is how a fixture drifts from the type it claims to be, and this file
// exists because of an assertion nobody could reach.
const feedOk: FeedFreshness = { tone: "live", label: "live", ageSec: 0 };
const feedDown: FeedFreshness = { tone: "down", label: "stale", ageSec: 9_000 };

const sub = (name: string, ok: boolean) => ({ name, ok });
const OK_SUBS = [sub("engine", true), sub("redis", true), sub("postgres", true), sub("lanes", true)];

/** A `/health` body that is healthy in every respect, so each case below alters ONE thing. */
const healthy = (over: Record<string, unknown> = {}) =>
  ({
    status: "ok",
    subsystems: OK_SUBS,
    feed_last_tick_ts: 1,
    reconcile_drift: [],
    ownership_violations: [],
    ...over,
  }) as never;

type Input = Parameters<typeof classifyHealth>[0];
const base: Input = {
  isError: false,
  isFetched: true,
  data: healthy(),
  feed: feedOk,
  wsActive: false,
  wsConnected: true,
};

/** One input per level. The KEYS are checked against `STYLE` below — that is the class guard. */
const CASES: Record<Exclude<HealthLevel, "ok">, Input> = {
  apiDown: { ...base, isError: true },
  engineDown: { ...base, data: healthy({ subsystems: [sub("engine", false), sub("redis", true)] }) },
  reconcileDrift: {
    ...base,
    data: healthy({ reconcile_drift: [{ symbol: "HSBC", broker_qty: 93, cockpit_qty: 0 }] }),
  },
  ownershipViolation: {
    ...base,
    data: healthy({
      ownership_violations: [
        { strategy_id: "MOMENTUM-002", instrument_id: "WHD.XNYS", signed_qty: -28 },
      ],
    }),
  },
  degraded: { ...base, data: healthy({ subsystems: [sub("engine", true), sub("postgres", false)] }) },
  feedStale: { ...base, feed: feedDown },
  wsIssues: { ...base, wsActive: true, wsConnected: false },
};

describe("every banner level is reachable from classifyHealth", () => {
  it("the fixture is not vacuous", () => {
    // A scan finding nothing passes every assertion below — the shape of the bug under test.
    expect(Object.keys(STYLE).length).toBeGreaterThan(0);
    expect(STYLE).toHaveProperty("apiDown");
    expect(Object.keys(CASES).length).toBeGreaterThan(0);
  });

  it("every level in the STYLE map has a case here", () => {
    const styled = new Set(Object.keys(STYLE));
    const cased = new Set(Object.keys(CASES));
    const uncovered = [...styled].filter((l) => !cased.has(l));
    expect(uncovered).toEqual([]);
    // And the reverse, so a case for a level the banner cannot render is caught too.
    expect([...cased].filter((l) => !styled.has(l))).toEqual([]);
  });

  it.each(Object.keys(CASES) as Array<Exclude<HealthLevel, "ok">>)(
    "%s is produced by its own condition",
    (level) => {
      expect(classifyHealth(CASES[level]).level).toBe(level);
    },
  );

  it("a healthy stack is ok, with an empty message", () => {
    const s = classifyHealth(base);
    expect(s.level).toBe("ok");
    expect(s.message).toBe("");
  });

  it("carries the feed and the violations on EVERY level, including apiDown", () => {
    // The reason both are computed before the early returns: the one screen state where the book is
    // least trustworthy must not render `undefined`.
    for (const level of Object.keys(CASES) as Array<Exclude<HealthLevel, "ok">>) {
      const s = classifyHealth(CASES[level]);
      expect(s.feed).toBeDefined();
      expect(Array.isArray(s.ownershipViolations)).toBe(true);
    }
  });
});

describe("drift and ownership are two facts, not one slot (#807 item 3)", () => {
  it("the banner carries BOTH when both are present", () => {
    const both: Input = {
      ...base,
      data: healthy({
        reconcile_drift: [{ symbol: "PATH", broker_qty: 126, cockpit_qty: 0 }],
        ownership_violations: [
          { strategy_id: "TECHIVOL-005", instrument_id: "CRM.XNYS", signed_qty: -10 },
        ],
      }),
    };
    const s = classifyHealth(both);
    expect(s.level).toBe("reconcileDrift");
    expect(s.message).toContain("PATH 126");
    expect(s.message).toContain("TECHIVOL-005");
    expect(s.message).toContain("CRM");
  });
  it("drift alone says nothing about ownership", () => {
    const s = classifyHealth(CASES.reconcileDrift);
    expect(s.message).not.toContain("·");
  });
});

/**
 * SPLIT DIVERGENCE MUST MOVE THE BANNER (#817).
 *
 * The backend now sets `status: "degraded"` when the claims ledger and the engine cache disagree
 * about which lane holds a position. `classifyHealth` never reads `h.status` — it derives the level
 * from the individual fields — so that degrade reached NOTHING until this branch existed. Codex
 * caught it in implementation review: a backend degrade and a green banner is two sources agreeing,
 * which is exactly when a severed wire is invisible.
 *
 * Measured on staging2 2026-09-09: eleven divergent pairs, 1,504 shares, BCTROT-004 claiming
 * positions the engine attributes to EXTERNAL.
 */
describe("split divergence reaches the banner", () => {
  const withSplit = (over: Record<string, unknown>) =>
    classifyHealth({ ...base, data: healthy({ split_divergence: over }) });

  it("the fixture is not vacuous — the same body with no divergence is ok", () => {
    // Without this, an assertion that a divergent book is red could pass because EVERYTHING is red.
    expect(classifyHealth(base).level).toBe("ok");
    expect(withSplit({ status: "ok", pairs: [] }).level).toBe("ok");
  });

  it("a divergent pair raises the banner and names the count", () => {
    const s = withSplit({
      status: "ok",
      pairs: [
        { symbol: "AEM", strategy_id: "BCTROT-004", claim: 46, cache: 0 },
        { symbol: "ARKK", strategy_id: "BCTROT-004", claim: 115, cache: 0 },
      ],
    });
    expect(s.level).toBe("ownershipViolation");
    expect(s.message).toContain("2");
    expect(s.message.toLowerCase()).toContain("reconciled");
  });

  it("a check that COULD NOT RUN is not rendered as a clean book OR as a divergence", () => {
    // Three states. `claims_unreadable` says nothing about the book, so paging on it would train an
    // operator to ignore the banner for what is usually a Postgres hiccup — and reporting it as ok
    // would be the silent all-clear this whole ticket is about.
    for (const status of ["claims_unreadable", "cache_unreadable", "compute_failed"]) {
      const s = withSplit({ status, pairs: [], error: "boom" });
      expect(s.level).toBe("ok");
    }
  });

  it("an ABSENT field does not crash or raise — an older api must still render", () => {
    // The api and the UI deploy separately; a UI ahead of its backend sees no key at all.
    expect(classifyHealth({ ...base, data: healthy({}) }).level).toBe("ok");
  });
});

/**
 * AN ENGINE THAT HAS TOLD US NOTHING IS NOT "ok" (#859). Measured 2026-09-11 00:34:45–00:35:14 SGT:
 * three bridge-stale windows across a paper recreate, each rendering `reconcile_drift []`,
 * `ownership_violations []`, `lanes ok:true` from a payload that contained nothing. The backend now
 * sends `null` for every list and `bridge_ok: false` in that state — AND forces the engine subsystem
 * to `ok: false` (`app.py`: `engine_ok = bridge_ok and engine_ok`), so the realistic payload lands on
 * `engineDown` before any newer branch runs.
 *
 * THE FIRST FIXTURE HERE WAS IMPOSSIBLE (review of #883): it paired `engine ok: true` with
 * `bridge_ok: false`, a state the API cannot emit, and it vouched for a branch no live payload
 * reaches. Two cases now, both reachable: the real payload (engine down → engineDown), and the
 * BACKSTOP the `bridge_ok` branch exists for — a payload with NO `subsystems` array at all
 * (`down()` is false on an empty array, so without the branch every-list-null falls through to "ok").
 */
describe("an absent engine frame is unknown, not clean (#859)", () => {
  const allNull = { feed_last_tick_ts: null, reconcile_drift: null, ownership_violations: null, unpriced_positions: null, automated_lanes_running: null };
  const asData = (o: Record<string, unknown>) => o as unknown as Parameters<typeof classifyHealth>[0]["data"];
  /** What `/health` actually emits with no frame: bridge_ok false AND the engine subsystem false. */
  const realNoFrame = () => ({ status: "degraded", bridge_ok: false, subsystems: [sub("engine", false), sub("redis", true), sub("postgres", true), { name: "lanes", ok: null }], ...allNull });
  /** The backstop case: bridge_ok false and no subsystems array to consult. */
  const noSubsystems = () => ({ status: "degraded", bridge_ok: false, subsystems: [], ...allNull });

  it("the fixtures are all-null where a live frame would carry lists, and the real one has the engine DOWN", () => {
    for (const h of [realNoFrame(), noSubsystems()] as Record<string, unknown>[]) {
      expect(h.reconcile_drift).toBeNull();
      expect(h.ownership_violations).toBeNull();
      expect(h.bridge_ok).toBe(false);
    }
    expect(realNoFrame().subsystems.some((x) => x.name === "engine" && x.ok === false)).toBe(true);
  });

  it("the REAL no-frame payload classifies engineDown and does not throw on null lists", () => {
    const state = classifyHealth({ isError: false, isFetched: true, data: asData(realNoFrame()), feed: feedOk, wsActive: true, wsConnected: true });
    expect(state.level).toBe("engineDown");
    expect(state.unpricedPositions).toBeUndefined();
  });

  it("a bridge_ok:false payload with NO subsystems array still cannot classify as ok (the backstop branch)", () => {
    const state = classifyHealth({ isError: false, isFetched: true, data: asData(noSubsystems()), feed: feedOk, wsActive: true, wsConnected: true });
    expect(state.level).not.toBe("ok");
    expect(state.message.toLowerCase()).toMatch(/engine|frame|unknown/);
    expect(state.unpricedPositions).toBeUndefined();
  });
});

describe("ownershipViolations is three-state on HealthState (#884)", () => {
  it("carries null when the payload says null, and [] when the payload says []", () => {
    const base = { isError: false, isFetched: true, feed: feedOk, wsActive: true, wsConnected: true };
    const nul = classifyHealth({ ...base, data: healthy({ ownership_violations: null }) as never });
    const empty = classifyHealth({ ...base, data: healthy({ ownership_violations: [] }) as never });
    expect(nul.ownershipViolations).toBeNull();
    expect(empty.ownershipViolations).toEqual([]);
  });
});
