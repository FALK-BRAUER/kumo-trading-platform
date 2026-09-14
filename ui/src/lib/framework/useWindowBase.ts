/**
 * Fetching the per-lane window base (#699).
 *
 * ITS OWN QUERY, NOT PART OF THE FRAME. `/pnl/unrealized-base` reads a database and its answer
 * changes once a day, at the venue close. The trade frame is pushed continuously; joining this to it
 * would put a Postgres round trip on the hot path for a number that does not move.
 *
 * ALL PERIODS ARRIVE TOGETHER, so switching the selector cannot briefly show one window's base
 * against another's standing figure.
 */

import { useQuery } from "@tanstack/react-query";

import { API_BASE } from "@/lib/config";
import type { components } from "@/lib/api/schema";

export type WindowBase = components["schemas"]["WindowBaseResponse"];

/** How often to re-ask. The answer changes once a day; this is a safety net, not a poll. */
const REFETCH_MS = 5 * 60_000;

export function useWindowBase(): WindowBase | null {
  const query = useQuery({
    queryKey: ["pnl-unrealized-base"],
    queryFn: async (): Promise<WindowBase> => {
      const res = await fetch(`${API_BASE}/pnl/unrealized-base`, { cache: "no-store" });
      if (!res.ok) throw new Error(`unrealized-base ${res.status}`);
      return (await res.json()) as WindowBase;
    },
    refetchInterval: REFETCH_MS,
    retry: false,
  });
  // NULL WHILE UNKNOWN, never a fabricated empty shape. An `{by_period:{}}` placeholder would make
  // every lane's base "absent from a captured day", which `windowDelta` reads as a KNOWN ZERO — a
  // fetch that has not returned yet would render as "the mark did not move". The endpoint's own
  // three states only survive if the not-yet-answered case is distinct from all of them.
  return query.data ?? null;
}
