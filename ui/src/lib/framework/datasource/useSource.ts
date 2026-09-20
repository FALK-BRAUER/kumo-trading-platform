"use client";

/**
 * useSource / useSources — resolve a tile's declared data sources to `{ data, status }`. Tiles never
 * call this directly; the TileContainer does, and passes the result down as props.
 *
 * `useSource` switches on the source `kind` INTERNALLY — so moving a source rest → ws/hybrid (#7
 * phase 3) is invisible to tiles. REST is the only path wired now; ws/hybrid return a loading stub
 * until the WS manager lands. The query key `['source', name, params]` is the shared identity the WS
 * phase will reuse (`setQueryData`) to freshen the same cache entry.
 */
import { useCallback, useSyncExternalStore } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { API_BASE } from "@/lib/config";
import { normaliseQuantity } from "@/lib/api/normaliseQuantity";
import type { SourceStatus } from "../types";
import { getSource, type DataSource } from "./registry";
import { canonicalTopicKey, type Channel, type Topic } from "./protocol";
import { wsManager } from "./ws-manager";

const DEFAULT_STALE_MS = 2_000;
const DEFAULT_GC_MS = 60_000;

async function fetchRest(endpoint: string, params: Record<string, unknown>): Promise<unknown> {
  const url = new URL(`${API_BASE}${endpoint}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  const res = await fetch(url.toString(), { cache: "no-store" });
  if (!res.ok) throw new Error(`GET ${endpoint} failed: ${res.status}`);
  // ONE decode, one normalisation (#855) — see `normaliseQuantity` for why this is not a per-reader rule.
  return normaliseQuantity(await res.json());
}

/** Minimal shape of what we need off a query result to derive tile status (REST or future WS). */
interface SourceResult {
  data: unknown;
  isPending: boolean;
  isError: boolean;
  isStale: boolean;
}

/** TanStack flags → the tile-facing enum. The ONE place this mapping lives. */
export function mapToTileStatus(q: SourceResult): SourceStatus {
  if (q.isError && q.data === undefined) return "error";
  if (q.isPending && q.data === undefined) return "loading";
  if (q.isError && q.data !== undefined) return "stale"; // surface last value, flagged stale
  if (q.isStale) return "stale";
  return "live";
}

function useRestSource(def: DataSource<unknown>, cfg: unknown): SourceResult {
  const params = def.params?.(cfg) ?? {};
  const q: UseQueryResult<unknown> = useQuery({
    queryKey: ["source", def.name, params],
    queryFn: () => fetchRest(def.endpoint as string, params),
    staleTime: def.cacheMs ?? DEFAULT_STALE_MS,
    gcTime: def.gcMs ?? DEFAULT_GC_MS,
  });
  return { data: q.data, isPending: q.isPending, isError: q.isError, isStale: q.isStale };
}

/** WS/hybrid path — subscribe via the manager, read its per-topic view through useSyncExternalStore. */
function useWsSource(def: DataSource<unknown>, cfg: unknown): SourceResult {
  const topic: Topic = { channel: def.channel as Channel, params: def.params?.(cfg) as Record<string, string> | undefined };
  const key = canonicalTopicKey(topic);

  // Ref-counted subscribe on mount / unsubscribe on unmount; re-subscribe if the topic key changes.
  const subscribe = useCallback(
    (onChange: () => void) => {
      const releaseTopic = wsManager.subscribe(topic);
      const releaseListener = wsManager.onChange(onChange);
      return () => {
        releaseListener();
        releaseTopic();
      };
    },
    // topic is rebuilt each render but only its canonical key matters for identity.
    [key], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const getSnapshot = useCallback(() => wsManager.getTopicState(key), [key]);
  const state = useSyncExternalStore(subscribe, getSnapshot, () => undefined);

  return {
    data: state?.data,
    isPending: !state || (state.replayPhase !== "live" && state.data === undefined),
    isError: !!state?.error,
    isStale: false,
  };
}

/** Stub for a source kind with no transport wired. Holds 'loading', calls no extra hooks. */
function useStubSource(): SourceResult {
  return { data: undefined, isPending: true, isError: false, isStale: false };
}

/**
 * Resolve a single source. Hook order is stable per source name (kind is fixed in the registry), so
 * the internal branch is rules-of-hooks safe.
 */
export function useSource(name: string, cfg: unknown): { data: unknown; status: SourceStatus } {
  const def = getSource(name);
  let result: SourceResult;
  if (def?.kind === "rest") result = useRestSource(def, cfg);
  else if (def && (def.kind === "ws" || def.kind === "hybrid")) result = useWsSource(def, cfg);
  else result = useStubSource();
  const status: SourceStatus = !def ? "error" : mapToTileStatus(result);
  return { data: result.data, status };
}

/**
 * Resolve a tile's whole `dataSources` list. The list is static per tile type (stable length/order),
 * so looping `useSource` here is rules-of-hooks safe. Single integration point for the WS phase.
 */
export function useSources(
  names: string[],
  cfg: unknown,
): { data: Record<string, unknown>; status: Record<string, SourceStatus> } {
  const data: Record<string, unknown> = {};
  const status: Record<string, SourceStatus> = {};
  for (const name of names) {
    // eslint-disable-next-line react-hooks/rules-of-hooks -- names is static per tile type (see doc)
    const resolved = useSource(name, cfg);
    data[name] = resolved.data;
    status[name] = resolved.status;
  }
  return { data, status };
}
