/** ONE read of `/strategies`, shared by every hook that renders a lane's declared facts (#888 review).
 *
 * `useSleeves`, `useLaneCadence` and `useTransferTargets` all read this endpoint. Separate query keys
 * would be separate caches of one payload, refreshed on separate clocks — and a lane cell would then
 * show a sleeve from one snapshot beside a cadence from another. One key, one snapshot; the hooks are
 * selectors over it.
 *
 * Failure yields `undefined`, and each selector decides what an absent payload means for ITS field —
 * `—` for a percentage, `cadence —` for the note — never a default that reads as a healthy value.
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { API_BASE } from "@/lib/config";

const POLL_MS = 60_000; // declared facts change when an operator redeclares them, not tick by tick

export interface StrategyRow {
  strategy_id?: string;
  target?: number | null;
  cadence?: string | null;
  next_rebalance?: string | null;
  /** The bridge's three-valued arm row (#997). `state` is `"unknown"` for a lane the registry knows
   *  but this node did not build (CRSISHORT-006 on paper) AND for the manual lane, which never arms;
   *  `laneChips.laneIsOnTheFrame` is the one reader that tells those two apart (#1099). */
  arm?: { state?: string | null } | null;
  /** #873 phase 1 — the lane's market-aware readings; `null` when the engine frame could not be read. */
  market_aware?: LaneMarketAware | null;
}

/** One hook's reading (#873). `state` is what cockpit branches on — FIVE values; `observed` is what the
 *  hook itself said and is display-only. */
export interface HookReading {
  state: string;
  observed?: string;
  reasons?: string[];
  /** The action the answer named (`stand_down` / `investigate` / …) — display only. */
  action?: string | null;
  polls?: number;
  unknowns?: number;
  faults?: number;
  in_episode?: boolean;
  dwell?: number;
  asked_at_ns?: number;
}

/** The per-lane container on `/strategies` (#873): three states OUTSIDE (the whole thing `null` = the
 *  engine frame could not be read / the build lacks the plane) and INSIDE (`lane` null = the plane is
 *  up and this lane has not been polled). `contract.state` names whether the strategies pin carries the
 *  vocabulary at all. */
export interface LaneMarketAware {
  contract?: { state?: string; module?: string; reason?: string } | null;
  dwell?: number | null;
  polled_at_ns?: number | null;
  lane?: {
    entries_blocked?: HookReading;
    emergency_exit?: HookReading;
    self_assessment?: HookReading;
    polled_at_ns?: number;
  } | null;
}

export interface StrategyRows {
  /** `undefined` while loading or after a failed read; the selectors decide what absence means. */
  rows: StrategyRow[] | undefined;
  /** The read FAILED — distinct from "not yet", for the one selector that names that state. */
  failed: boolean;
  /** Whether the rows' `arm` state was READ from the engine frame or DEFAULTED (#1099): `"ok"` |
   *  `"unreadable"`; `undefined` while loading, after a failure, or from an older api that does not
   *  say. A reader treats anything but `"ok"` as "could not ask" — never as "no lane is here". */
  frame: "ok" | "unreadable" | undefined;
}

export function useStrategyRows(): StrategyRows {
  const query = useQuery({
    queryKey: ["strategies", "rows"],
    queryFn: async (): Promise<{ rows: StrategyRow[]; frame: "ok" | "unreadable" | undefined }> => {
      const res = await fetch(`${API_BASE}/strategies`, { cache: "no-store" });
      if (!res.ok) throw new Error(`strategies ${res.status}`);
      const body = (await res.json()) as { strategies?: StrategyRow[]; engine_frame?: string };
      // Only the two spellings the api emits; anything else (an older api, a typo) is "did not say".
      const frame = body.engine_frame === "ok" || body.engine_frame === "unreadable" ? body.engine_frame : undefined;
      return { rows: body.strategies ?? [], frame };
    },
    refetchInterval: POLL_MS,
    retry: false,
    placeholderData: keepPreviousData,
  });
  return { rows: query.data?.rows, failed: query.isError, frame: query.data?.frame };
}
