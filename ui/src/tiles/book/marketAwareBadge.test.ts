/** The lane's market-aware badge (#873): three states at every level, a tone for EVERY state, and the
 * calm tone only for a lane that answered `no` on what it implements. `null`, `unknown`, `fault` and a
 * contract absent from the pin are each their own state — a plane present and reading nothing yet must
 * never look like present and clean (#859's rule, one plane over).
 */
import { describe, expect, it } from "vitest";

import { ACTIONS, CONTAINER_STATES, READING_STATES, TONE, YES_LABEL, marketAwareBadge } from "./marketAwareBadge";

const present = { contract: { state: "present", module: "kumo_strategies.strategies.market_events" }, dwell: 3 };
const reading = (state: string, extra: Record<string, unknown> = {}) => ({ state, reasons: ["r"], ...extra });

describe("the tone map is enumerated over the whole state union", () => {
  it("every state has a tone and only `no` is the calm one", () => {
    const all = [...READING_STATES, ...CONTAINER_STATES];
    for (const s of all) expect(TONE[s], s).toBeTruthy();
    expect(Object.keys(TONE).sort()).toEqual([...all].sort());
    const calm = TONE.no;
    for (const s of all) if (s !== "no") expect(TONE[s], `${s} must not render calm`).not.toBe(calm);
  });

  it("the action union spans BOTH upstream enums and never says quarantine", () => {
    expect([...ACTIONS]).toEqual(["exit_only", "liquidate", "stand_down", "investigate"]);
    for (const v of Object.values(YES_LABEL)) expect(v.toLowerCase()).not.toContain("quarantine");
  });
});

describe("three states outside", () => {
  it("null / undefined is UNREAD, its own state", () => {
    for (const row of [null, undefined]) {
      const b = marketAwareBadge(row);
      expect(b.state).toBe("unread");
      expect(b.text).toBe("market view —");
      expect(b.tone).not.toBe(TONE.no);
    }
  });

  it("a contract absent from the pin is NAMED, with the module", () => {
    const b = marketAwareBadge({ contract: { state: "absent", module: "kumo_strategies.strategies.market_events" }, lane: null });
    expect(b.state).toBe("contract_absent");
    expect(b.title).toContain("kumo_strategies.strategies.market_events");
    expect(b.title).toContain("read nothing yet");
  });

  it("plane up, lane not yet polled", () => {
    const b = marketAwareBadge({ ...present, lane: null });
    expect(b.state).toBe("never_polled");
  });
});

describe("three states inside", () => {
  it("a lane implementing nothing reads NOT DECLARED, not clean", () => {
    const b = marketAwareBadge({ ...present, lane: { entries_blocked: reading("not_asked"), emergency_exit: reading("not_asked"), self_assessment: reading("not_asked") } });
    expect(b.state).toBe("not_asked");
    expect(b.tone).not.toBe(TONE.no);
  });

  it("entries blocked says what is true of the lane, in the contract's words", () => {
    const b = marketAwareBadge({ ...present, lane: { entries_blocked: reading("yes", { reasons: ["index below its 50-session average"] }), emergency_exit: reading("no"), self_assessment: reading("no") } });
    expect(b.text).toBe("not opening new positions");
    expect(b.title).toContain("index below its 50-session average");
    expect(b.title).toContain("nothing acted");
  });

  it("an emergency in episode is loudest and says closing its book with its dwell", () => {
    const b = marketAwareBadge({ ...present, lane: { entries_blocked: reading("yes"), emergency_exit: reading("yes", { in_episode: true, polls: 3, dwell: 3, reasons: ["crash"] }), self_assessment: reading("yes") } });
    expect(b.text).toBe("closing its book");
    expect(b.tone).toBe(TONE.fault);
    expect(b.title).toContain("3 consecutive polls");
  });

  it("an emergency yes BELOW the dwell is shown counting, not as closing", () => {
    const b = marketAwareBadge({ ...present, lane: { entries_blocked: reading("no"), emergency_exit: reading("yes", { in_episode: false, polls: 1, dwell: 3 }), self_assessment: reading("no") } });
    expect(b.text).toBe("emergency_exit yes 1/3");
  });

  it("stand-down uses STAND_DOWN wording, never quarantine", () => {
    const b = marketAwareBadge({ ...present, lane: { entries_blocked: reading("no"), emergency_exit: reading("no"), self_assessment: reading("yes", { reasons: ["sharpe -0.4 over 9 windows"] }) } });
    expect(b.text).toContain("requesting stand-down");
    expect(b.text.toLowerCase()).not.toContain("quarantine");
  });

  it("a fault outranks everything and carries the count", () => {
    const b = marketAwareBadge({ ...present, lane: { entries_blocked: reading("yes"), emergency_exit: reading("fault", { faults: 7, reasons: ["RuntimeError: bar deque is empty"] }), self_assessment: reading("no") } });
    expect(b.state).toBe("fault");
    expect(b.text).toBe("emergency_exit raising ×7");
    expect(b.title).toContain("bar deque is empty");
  });

  it("unknown is its own state with its count and reason — not fine", () => {
    const b = marketAwareBadge({ ...present, lane: { entries_blocked: reading("unknown", { unknowns: 2, reasons: ["12 sessions, 51 needed"] }), emergency_exit: reading("no"), self_assessment: reading("no") } });
    expect(b.state).toBe("unknown");
    expect(b.text).toBe("entries_blocked: unknown ×2");
    expect(b.tone).not.toBe(TONE.no);
  });

  it("only a lane answering no on what it implements reads clear", () => {
    const b = marketAwareBadge({ ...present, lane: { entries_blocked: reading("no"), emergency_exit: reading("no"), self_assessment: reading("not_asked") } });
    expect(b.state).toBe("no");
    expect(b.text).toBe("market view: clear");
  });
});


describe("the high tail: investigate is a look, not an alarm", () => {
  it("a self-assessment above its envelope says outperformance, not stand-down, in a non-alarm tone", () => {
    const b = marketAwareBadge({ ...present, lane: { entries_blocked: reading("no"), emergency_exit: reading("no"), self_assessment: reading("yes", { action: "investigate", reasons: ["return +30% above p90"] }) } });
    expect(b.text).toContain("unexplained outperformance");
    expect(b.text.toLowerCase()).not.toContain("stand-down");
    expect(b.tone).not.toBe(TONE.fault);
    expect(b.tone).not.toBe(TONE.yes);
  });
  it("the action union carries investigate", () => {
    expect([...ACTIONS]).toContain("investigate");
  });
});
