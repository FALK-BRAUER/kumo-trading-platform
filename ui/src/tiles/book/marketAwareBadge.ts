/** What a lane cell says about the lane's market view (#873 phase 1). Pure; rendered beside the cadence.
 *
 * THREE STATES AT EVERY LEVEL, and the tone map is ENUMERATED over the whole state union so a state
 * added tomorrow — a new hook answer, a new action — fails `marketAwareBadge.test.ts` rather than
 * rendering as a blank or, worse, as the calm tone. `null`, `unknown`, `fault` and a contract that is
 * ABSENT from the strategies pin are each their own state and never the clean one: a plane that is
 * present and reading nothing YET must not look like present and clean.
 *
 * WORDS. Labels mirror kumo-strategies' `market_events.LABELS` (pinned equal from the backend, see
 * `test_market_aware_ui_pin.py`): they say what is TRUE OF THE LANE, not what the platform did. The lane
 * self-report is STAND_DOWN — never "quarantine", which is this repo's word for the #79 plane
 * (foreign broker activity, "UNCLAIMED · foreign strategy"). Two meanings of one word must not meet
 * on one surface.
 */
import type { LaneMarketAware } from "@/lib/framework/useStrategyRows";

/** The five hook-reading states, exactly the backend's `READING_STATES`. */
export const READING_STATES = ["not_asked", "unknown", "fault", "yes", "no"] as const;
/** Badge-level states beyond a single reading. */
export const CONTAINER_STATES = ["unread", "never_polled", "contract_absent"] as const;
/** The union of both upstream action enums: `MarketAction` (exit_only, liquidate) and `SelfAction`
 *  (stand_down). Two enums, deliberately — a market view must never be able to stand a lane down. */
export const ACTIONS = ["exit_only", "liquidate", "stand_down", "investigate"] as const;

export type BadgeState = (typeof READING_STATES)[number] | (typeof CONTAINER_STATES)[number];

/** Tailwind text tone per state. EVERY state has an entry; the test enumerates. `no` is the only calm one. */
export const TONE: Record<BadgeState, string> = {
  unread: "italic text-zinc-400",
  never_polled: "text-zinc-400",
  contract_absent: "text-sky-500",
  not_asked: "text-zinc-600",
  unknown: "text-amber-400",
  fault: "text-red-400",
  yes: "text-amber-400",
  no: "text-zinc-500",
};

/** Mirrors `market_events.LABELS` for the acting answers, per hook. */
export const YES_LABEL = {
  entries_blocked: "not opening new positions",
  emergency_exit: "closing its book",
  self_assessment: "requesting stand-down (outside its own envelope)",
} as const;

/** The high tail of a self-assessment: the lane is ABOVE its own envelope. A reason to look — sizing,
 *  data, config — never an alarm about the lane's health, and never a reason to reduce. A badge that
 *  reads as an alarm on a lane up 30% gets switched off, and STAND_DOWN goes with it. */
export const INVESTIGATE_LABEL = "unexplained outperformance — check sizing/data/config";

export interface MarketAwareBadge {
  text: string;
  title: string;
  tone: string;
  state: BadgeState;
}

const HOOKS = ["emergency_exit", "entries_blocked", "self_assessment"] as const;

export function marketAwareBadge(row: LaneMarketAware | null | undefined): MarketAwareBadge {
  if (row === null || row === undefined) {
    return {
      state: "unread",
      tone: TONE.unread,
      text: "market view —",
      title: "The engine's market-aware frame could not be read, or this build has no such plane. Unknown, not clean.",
    };
  }
  const contract = row.contract?.state ?? "absent";
  if (contract !== "present") {
    return {
      state: "contract_absent",
      tone: TONE.contract_absent,
      text: "market view: contract absent",
      title:
        `The strategies pin on this instance does not carry ${row.contract?.module ?? "the market-events contract"}` +
        ` — the plane is up and can read nothing yet. Reads present → absent again on the pin bump.` +
        (row.contract?.reason ? ` (${row.contract.reason})` : ""),
    };
  }
  if (!row.lane) {
    return {
      state: "never_polled",
      tone: TONE.never_polled,
      text: "market view: not yet polled",
      title: "The plane is up; this lane has not been asked yet (or is not registered on the node).",
    };
  }
  // Loudest first: an acting emergency, then a fault, then blocked entries, then stand-down, then unknown.
  const readings = HOOKS.map((h) => [h, row.lane?.[h]] as const);
  const faults = readings.filter(([, r]) => r?.state === "fault");
  if (faults.length) {
    const [h, r] = faults[0];
    return {
      state: "fault",
      tone: TONE.fault,
      text: `${h} raising${(r?.faults ?? 0) > 1 ? ` ×${r?.faults}` : ""}`,
      title: `Its ${h} hook RAISED on ${r?.faults ?? 1} consecutive poll(s) — that protection is absent. ` +
        (r?.reasons ?? []).join(" · "),
    };
  }
  for (const h of HOOKS) {
    const r = row.lane?.[h];
    if (r?.state === "yes" && (h !== "emergency_exit" || r.in_episode)) {
      const investigate = h === "self_assessment" && r.action === "investigate";
      return {
        state: "yes",
        tone: h === "emergency_exit" ? TONE.fault : investigate ? TONE.contract_absent : TONE.yes,
        text: investigate ? INVESTIGATE_LABEL : YES_LABEL[h],
        title: `${h}: yes` + (h === "emergency_exit" ? ` for ${r.polls ?? 0} consecutive polls (dwell ${r.dwell ?? "?"})` : "") +
          ` — ${(r.reasons ?? []).join(" · ") || "no reason given"}. Phase 1: reported, nothing acted.`,
      };
    }
  }
  const emergencyPending = row.lane.emergency_exit;
  if (emergencyPending?.state === "yes") {
    return {
      state: "yes",
      tone: TONE.yes,
      text: `emergency_exit yes ${emergencyPending.polls ?? 0}/${emergencyPending.dwell ?? "?"}`,
      title: `emergency_exit is answering yes but has not dwelled ${emergencyPending.dwell ?? "?"} polls yet — ` +
        (emergencyPending.reasons ?? []).join(" · "),
    };
  }
  const unknowns = readings.filter(([, r]) => r?.state === "unknown");
  if (unknowns.length) {
    const [h, r] = unknowns[0];
    return {
      state: "unknown",
      tone: TONE.unknown,
      text: `${h}: unknown${(r?.unknowns ?? 0) > 1 ? ` ×${r?.unknowns}` : ""}`,
      title: `The lane could not compute ${h} — ${(r?.reasons ?? []).join(" · ") || "no reason given"}. Honest, and common for a new lane; not "fine".`,
    };
  }
  if (readings.every(([, r]) => !r || r.state === "not_asked")) {
    return {
      state: "not_asked",
      tone: TONE.not_asked,
      text: "market view: not declared",
      title: "This lane implements none of entries_blocked / emergency_exit / self_assessment. Reported, not assumed clean.",
    };
  }
  return {
    state: "no",
    tone: TONE.no,
    text: "market view: clear",
    title: readings.map(([h, r]) => `${h}: ${r?.state ?? "not_asked"}`).join(" · "),
  };
}
