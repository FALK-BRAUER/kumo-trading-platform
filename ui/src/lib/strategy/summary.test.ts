import { describe, expect, it } from "vitest";

import type { SessionFrame } from "@/lib/framework/datasource/protocol";
import { plannedActions, summarise } from "./summary";

/** The real 2026-08-10 frame, trimmed. It carries the intent-vs-outcome trap by accident. */
const FRAME = {
  strategy_id: "MOMENTUM-002",
  session: "2026-08-10",
  today: "2026-08-10",
  decision_is_today: true,
  lifecycle: { state: "TRADING", reason: "operator: start paper trading" },
  decision: {
    summary: "TRADING: hold 8 · enter 8 · exit 2",
    reasons: { MET: "left the ranking", PRU: "gave back all of a 0.7% peak" },
  },
  submitted_count: 8,
  journal: [
    { kind: "risk", summary: "skipped CHEF: position cap" },
    { kind: "risk", summary: "3 pool names are not subscribed in this node" },
    { kind: "order", summary: "BUY 459 CGAU: submitted via nautilus" },
  ],
  errors: [],
  trail: [
    { symbol: "AEM", entry: 176, peak: 176, qty: 54, sessions_held: 0, quality: "live" },
    { symbol: "OLD", entry: 50, peak: 50, qty: 10, sessions_held: 9, quality: "adopted" },
  ],
} as unknown as SessionFrame;

describe("intent vs outcome", () => {
  it("surfaces the gap between planned actions and submitted orders", () => {
    // "enter 8 · exit 2" = 10 planned; 8 submitted, because CHEF and BRK.B hit the position cap.
    // A tile showing only the summary would report a rotation that did not fully happen.
    const s = summarise(FRAME);
    expect(s.shortfall).toEqual({ planned: 10, submitted: 8 });
  });

  it("shows no shortfall when intent and outcome agree", () => {
    const f = { ...FRAME, submitted_count: 10 } as unknown as SessionFrame;
    expect(summarise(f).shortfall).toBeNull();
  });

  it("does not report MORE submissions than planned as a discrepancy", () => {
    // Bracket legs, retries and manager orders all add submissions the decision never promised.
    const f = { ...FRAME, submitted_count: 14 } as unknown as SessionFrame;
    expect(summarise(f).shortfall).toBeNull();
  });

  it("excludes HOLD from planned actions", () => {
    // Holding is the absence of an action; counting it would make every session look short.
    expect(plannedActions("TRADING: hold 8 · enter 8 · exit 2")).toBe(10);
    expect(plannedActions("TRADING: hold 8 · enter 0 · exit 0")).toBe(0);
  });

  it("reports no shortfall rather than a wrong one when the summary cannot be parsed", () => {
    expect(plannedActions("something else entirely")).toBeNull();
    const f = { ...FRAME, decision: { summary: "blocked by gate" } } as unknown as SessionFrame;
    expect(summarise(f).shortfall).toBeNull();
  });
});

describe("attention", () => {
  it("keeps a degraded-system risk row and drops normal policy", () => {
    // A position cap doing its job is not an incident. Treating it as one would cry wolf on every
    // capped day and train the operator to ignore the panel.
    const s = summarise(FRAME);
    expect(s.attention).toEqual(["3 pool names are not subscribed in this node"]);
    expect(s.allClear).toBe(false);
  });

  it("dedupes a condition the journal recorded more than once", () => {
    // Seen live: "bars missing for 3/65 pool names" arrived three times as three identical rows and
    // rendered three times, pushing the rest of the list off the tile.
    const f = {
      ...FRAME,
      journal: [
        { kind: "risk", summary: "bars missing for 3/65 pool names (95% coverage)" },
        { kind: "risk", summary: "bars missing for 3/65 pool names (95% coverage)" },
        { kind: "risk", summary: "bars missing for 3/65 pool names (95% coverage)" },
      ],
    } as unknown as SessionFrame;
    expect(summarise(f).attention).toEqual(["bars missing for 3/65 pool names (95% coverage)"]);
  });

  it("does not alert on a manual position the strategy simply does not own", () => {
    // Fires every session because FIG is held manually. An always-on alert teaches the operator to
    // skip the list; the case that matters (an unclaimed phantom) surfaces via reconcile_drift.
    const f = {
      ...FRAME,
      journal: [{ kind: "risk", summary: "1 account positions are not this strategy's — leaving them alone" }],
      trail: [],
    } as unknown as SessionFrame;
    // Subject is the attention list only — this frame still carries a shortfall, which correctly
    // suppresses the all-clear on its own.
    expect(summarise(f).attention).toEqual([]);
  });

  it("puts errors first", () => {
    const f = {
      ...FRAME,
      errors: [{ summary: "submit failed for AEM: insufficient buying power" }],
    } as unknown as SessionFrame;
    expect(summarise(f).attention[0]).toContain("submit failed");
  });

  it("tolerates errors arriving as plain strings", () => {
    const f = { ...FRAME, errors: ["boom"] } as unknown as SessionFrame;
    expect(summarise(f).attention).toContain("boom");
  });

  it("is all-clear only when nothing needs the operator", () => {
    const s = summarise(CLEAN);
    expect(s.attention).toEqual([]);
    expect(s.allClear).toBe(true);
  });

  it("does not treat a benign row as degraded just because it contains a keyword", () => {
    const f = {
      ...CLEAN,
      journal: [
        { kind: "risk", summary: "bar coverage ok" },
        { kind: "risk", summary: "no breach of the daily loss limit" },
      ],
    } as unknown as SessionFrame;
    expect(summarise(f).attention).toEqual([]);
  });
});

// A frame with genuinely nothing to report: today's decision, fully submitted, no flags.
const CLEAN = {
  ...FRAME,
  decision: { summary: "TRADING: hold 8 · enter 0 · exit 0" },
  submitted_count: 0,
  journal: [{ kind: "risk", summary: "skipped CHEF: position cap" }],
  trail: [{ symbol: "AEM", entry: 176, peak: 176, qty: 54, sessions_held: 0, quality: "live" }],
} as unknown as SessionFrame;

describe("false all-clear — the worst output this tile can produce", () => {
  // REVIEW: allClear originally checked only the attention list, so it printed "Nothing needs you."
  // beside a shortfall, beside a stale decision, and while the strategy sat HALTED.
  it("is suppressed by a shortfall", () => {
    expect(summarise(FRAME).shortfall).not.toBeNull();
    expect(summarise(FRAME).allClear).toBe(false);
  });

  it("is suppressed when the last decision is not today's", () => {
    const f = { ...CLEAN, decision_is_today: false } as unknown as SessionFrame;
    expect(summarise(f).allClear).toBe(false);
  });

  it.each(["HALTED", "LIQUIDATING"])("is suppressed while the strategy is %s", (state) => {
    const f = { ...CLEAN, lifecycle: { state, reason: "operator" } } as unknown as SessionFrame;
    expect(summarise(f).allClear).toBe(false);
  });

  it("is suppressed when the engine has stopped publishing", () => {
    // The consumer re-pushes the last frame forever and the WS layer reports no staleness, so a
    // DEAD engine looks exactly like a quiet one. Age comes from the engine's own clock, which
    // stops advancing the moment it dies.
    const now = 1_000_000_000_000;
    const fresh = { ...CLEAN, ts: (now - 10_000) * 1e6 } as unknown as SessionFrame;
    const dead = { ...CLEAN, ts: (now - 600_000) * 1e6 } as unknown as SessionFrame;
    expect(summarise(fresh, now).stale).toBe(false);
    expect(summarise(fresh, now).allClear).toBe(true);
    expect(summarise(dead, now).stale).toBe(true);
    expect(summarise(dead, now).allClear).toBe(false);
  });

  it("treats a frame with no timestamp as neither fresh nor stale", () => {
    expect(summarise(CLEAN).ageMs).toBeNull();
    expect(summarise(CLEAN).stale).toBe(false);
  });
});

describe("unprotected positions", () => {
  it("names held positions whose peak was never observed", () => {
    // These LOOK protected and are not: peak-relative exits are inert while quality is adopted.
    expect(summarise(FRAME).unprotected).toEqual(["OLD"]);
  });

  it("ignores an adopted trail for a position already flat", () => {
    const f = {
      ...FRAME,
      trail: [{ symbol: "GONE", entry: 5, peak: 5, qty: 0, sessions_held: 3, quality: "adopted" }],
    } as unknown as SessionFrame;
    expect(summarise(f).unprotected).toEqual([]);
  });
});

describe("degenerate states", () => {
  it("distinguishes NO FRAME YET from nothing happened", () => {
    // "waiting" and "all clear" must never render the same way — one means the bridge is silent.
    const s = summarise(null);
    expect(s.waiting).toBe(true);
    expect(s.allClear).toBe(false);
    expect(summarise({} as SessionFrame).waiting).toBe(true);
  });

  it("carries lifecycle even when no session has decided", () => {
    const f = {
      lifecycle: { state: "PAUSED", reason: "operator: paused" },
      session: null, decision: null, submitted_count: 0, journal: [], errors: [], trail: [],
    } as unknown as SessionFrame;
    const s = summarise(f);
    expect(s.state).toBe("PAUSED");
    expect(s.headline).toBe("");
    expect(s.shortfall).toBeNull();
  });

  it("flags a decision that is not today's", () => {
    // The frame carries the LAST decision, which on a blocked or holiday session is not today's.
    // Rendering it as current would show a stale rotation as live.
    const f = { ...FRAME, today: "2026-08-11", decision_is_today: false } as unknown as SessionFrame;
    expect(summarise(f).decisionIsToday).toBe(false);
  });

  it("strips the state prefix from the headline", () => {
    expect(summarise(FRAME).headline).toBe("hold 8 · enter 8 · exit 2");
  });
});
