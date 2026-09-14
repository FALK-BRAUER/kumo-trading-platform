"use client";

/**
 * useHealth (#26) — the ONE app-level connection/health truth. Polls `GET /health` (the api's observed
 * state — api up + Redis-bridge liveness + engine self-report + last live-tick) and folds it, plus the WS
 * transport state, into a single `HealthState` for the global banner. Authored once so every surface reads
 * the same truth; tiles never re-derive "is the backend down".
 *
 * Precedence (worst first): apiDown > engineDown > feedStale > wsIssues > ok. `wsIssues` counts ONLY when
 * the WS is down WHILE topics are subscribed — an idle/no-subscriber view is neutral (fixes the old
 * RefreshBar lie where an empty view showed red).
 */
import { useSyncExternalStore } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { API_BASE } from "@/lib/config";
import type { HealthResponse } from "@/lib/api/types";
import { wsManager } from "./datasource/ws-manager";
import { driftMessage } from "./drift";
import { ownershipMessage, type OwnershipViolation } from "./ownership";
import { isUsMarketOpen, marketSession } from "@/lib/market";
import { feedFreshness, type FeedFreshness } from "@/lib/feedFreshness";

export type HealthLevel = "ok" | "wsIssues" | "feedStale" | "degraded" | "reconcileDrift" | "ownershipViolation" | "engineDown" | "apiDown";

export interface HealthState {
  level: HealthLevel;
  /** Human label for the banner (empty when level === "ok"). */
  message: string;
  /** Is data arriving — the same derivation the banner gates on, exposed for the header chip (#384).
   *  Returned rather than recomputed by the caller: one poll, one rule, and the chip and the banner
   *  cannot disagree about whether the feed is alive. */
  feed: FeedFreshness;
  /** Lanes holding a short in a long-only book (#437), as `/health` reported them.
   *  Returned rather than re-fetched by consumers for the same reason `feed` is: the banner and any
   *  tile qualifying its own numbers must not disagree about which lanes are mis-attributed. */
  /** `null` = the engine did not say (no frame, #859/#884); `[]` = asked and clean. Never coerced. */
  ownershipViolations: OwnershipViolation[] | null;
  /**
   * Held instruments the ENGINE could not price (`unpriced_positions`). Carried through so the book's
   * SECURED tally can separate "protection has not covered this yet" from "it never can" — the engine
   * knows because it tried, and deriving it a second time in the UI is how two answers to one question
   * drift apart.
   *
   * `undefined` where the frame did not say. Not an empty list: an older or degraded read must not
   * assert that everything is priceable.
   */
  unpricedPositions?: string[];
}

const POLL_MS = 3_000;

export function useHealth(nowMs: number): HealthState {
  const query = useQuery({
    queryKey: ["health"],
    queryFn: async (): Promise<HealthResponse> => {
      const res = await fetch(`${API_BASE}/health`, { cache: "no-store" });
      if (!res.ok) throw new Error(`health ${res.status}`);
      return (await res.json()) as HealthResponse;
    },
    refetchInterval: POLL_MS,
    retry: false,
    placeholderData: keepPreviousData,
  });

  // WS transport state — only meaningful as a problem when something is actually subscribed.
  const wsConnected = useSyncExternalStore(
    (cb) => wsManager.onChange(cb),
    () => wsManager.getConnection() === "connected",
    () => true,
  );
  const wsActive = useSyncExternalStore(
    (cb) => wsManager.onChange(cb),
    () => wsManager.hasActiveSubscriptions(),
    () => false,
  );

  return classifyHealth({
    isError: query.isError,
    isFetched: query.isFetched,
    data: query.data,
    // The print type travels WITH the stamp (#834): 900s old is dead for REALTIME and healthy for
    // DELAYED, and a derivation that sees only the age cannot tell the two apart.
    feed: feedFreshness(query.data?.feed_last_tick_ts, nowMs, marketSession(nowMs), query.data?.market_data_type),
    wsActive,
    wsConnected,
  });
}

/** Everything the level decision depends on. Gathered by the hook, decided by `classifyHealth`. */
export interface HealthInputs {
  isError: boolean;
  isFetched: boolean;
  data: HealthResponse | undefined;
  feed: FeedFreshness;
  wsActive: boolean;
  wsConnected: boolean;
}

/**
 * The level decision, as a PURE FUNCTION (#437 follow-up).
 *
 * Extracted from `useHealth` because it was unreachable inside it: this repo tests pure functions,
 * has no `renderHook`, and its three render tests assert on cells rather than the banner — so
 * deleting ANY of the seven branches below left all 839 tests green. Seven levels unpinned, and this
 * PR was adding an eighth. See `healthLevels.test.ts`, which drives itself from the banner's own
 * style map so the next level added cannot slip in untested.
 *
 * The hook keeps the polling, the WS subscription and the clock. It decides nothing.
 *
 * PRECEDENCE, worst first: apiDown > engineDown > reconcileDrift > ownershipViolation > degraded >
 * feedStale > wsIssues > ok.
 */
export function classifyHealth({ isError, isFetched, data, feed, wsActive, wsConnected }: HealthInputs): HealthState {
  // Computed BEFORE the early returns so every state carries it — an api that is unreachable still has
  // an honest answer about the feed (there is none), and the chip must not vanish on the one screen
  // state where it matters most.

    // THREE STATES (#884): a `null` or absent list is the engine not having said, and it is carried
    // as `null` on every branch — including engineDown — so the book tile renders "ownership unknown"
    // rather than dropping its badge as if the book were clean.
    const ownershipViolations = data?.ownership_violations ?? null;
    // NOT `?? []`. An absent key means the engine did not tell us, and defaulting to empty would
    // silently assert every holding is priceable — the exact absence-reads-as-permission shape.
    const unpricedPositions = (data as { unpriced_positions?: string[] | null } | undefined)
      ?.unpriced_positions ?? undefined;                 // null (unknown, #859) carries as "did not say"

  if (isError || (isFetched && !data)) {
    return { level: "apiDown", message: "Cockpit API unreachable — reconnecting", feed, ownershipViolations, unpricedPositions };
  }
  const h = data;
  if (h) {
    const down = (s: string) => h.subsystems.some((x) => x.name === s && !x.ok);
    // Critical (red): no live data without the engine or the Redis bus.
    if (down("engine")) return { level: "engineDown", message: "Trading engine offline — data is not updating", feed, ownershipViolations, unpricedPositions };
    if (down("redis")) return { level: "engineDown", message: "Data bus (Redis) down — live data unavailable", feed, ownershipViolations, unpricedPositions };
    // AN ENGINE THAT HAS TOLD US NOTHING IS NOT "ok" (#859). `bridge_ok: false` is the consumer's own
    // word that no fresh engine frame is behind this payload; every list below is then `null`
    // (unknown). On the payload the API actually emits the engine subsystem is ALSO false (app.py
    // forces it), so `down("engine")` above wins and this branch is the BACKSTOP: a payload with no
    // `subsystems` array (truncated, older) would otherwise fall through to "ok" with every list null.
    // Judged BEFORE drift/ownership, which cannot be known here.
    if ((h as { bridge_ok?: boolean | null }).bridge_ok === false) {
      return { level: "degraded", message: "Engine has not reported — book unknown, not clean", feed, ownershipViolations, unpricedPositions };
    }
    // Reconciliation drift (red): the cockpit's book and the broker's disagree, in EITHER direction — an
    // empty book must never be mistaken for a flat account, and a phantom book must never be mistaken for
    // held size. `driftMessage` names the direction; see ./drift. Ranks just under a dead engine/bus
    // (which would explain it).
    const message = driftMessage(h.reconcile_drift);
    // BOTH FACTS, NEVER ONE SLOT (#807 item 3). On 2026-09-09 four lanes carried mirrored shorts while
    // PATH drifted; the banner showed PATH and nothing else, because precedence picked a message rather
    // than composing one. Drift stays the level (the louder fact); ownership rides on the same line.
    const ownershipToo = ownershipMessage(h.ownership_violations);
    if (message) {
      return {
        level: "reconcileDrift",
        message: ownershipToo ? `${message} · ${ownershipToo}` : message,
        feed, ownershipViolations, unpricedPositions,
      };
    }
    // Ownership violation (red): the totals match the broker and the per-lane split does not. Ranks
    // JUST UNDER drift because drift, when present, is the louder fact — but this is red for the
    // same reason drift is: a mis-stated held size is size the operator can act on and does not own.
    // It sits here rather than inside `driftMessage` because the remedy is the opposite one: drift
    // says trust the broker's number, this says trust NEITHER lane figure. See ./ownership.
    const ownership = ownershipMessage(h.ownership_violations);
    if (ownership) return { level: "ownershipViolation", message: ownership, feed, ownershipViolations, unpricedPositions };
    // SPLIT DIVERGENCE (red): the claims ledger and the engine cache disagree about WHICH lane holds
    // a position's shares, while the totals can agree (#692/#817). Ranks with `ownershipViolation`
    // and for the same reason — an exit sized off the claim sells another lane's shares under
    // NETTING — so it reads as red rather than amber.
    //
    // WITHOUT THIS THE BACKEND DEGRADE IS A DEAD MECHANISM. `/health` now sets `status: degraded`
    // when pairs are non-empty, and nothing here reads `h.status` — so eleven divergent pairs on
    // staging2 would still have rendered a green banner. Two sources agreeing (both "ok") is exactly
    // when a severed wire is invisible; codex caught this in implementation review.
    //
    // NON-OK STATUS IS NOT A DIVERGENCE. `claims_unreadable` / `cache_unreadable` / `compute_failed`
    // mean the check could not run — that is not a claim about the book, and rendering it as one
    // would page an operator for a Postgres hiccup.
    const split = (h as { split_divergence?: { status?: string; pairs?: unknown[] } }).split_divergence;
    if (split?.status === "ok" && (split.pairs?.length ?? 0) > 0) {
      const n = split.pairs?.length ?? 0;
      return {
        level: "ownershipViolation",
        message: `Claims ledger disagrees with the engine on ${n} lane/symbol pair${n === 1 ? "" : "s"} — do not exit these until reconciled`,
        feed,
        ownershipViolations,
        unpricedPositions,
      };
    }
    // App-data (amber): watchlist/history persistence, not the live trading view.
    if (down("postgres")) return { level: "degraded", message: "Database offline — watchlist/app data unavailable", feed, ownershipViolations, unpricedPositions };
    // ONE DERIVATION OF "IS THE FEED FEEDING", shared with the header's FEED chip (#384). This used to
    // compute its own tick age against its own threshold while the chip computed another — two answers
    // to one question, which is the failure this repo has measured repeatedly. `feedFreshness` is now
    // the only place that decides, and both consumers read it.
    //
    // BEHAVIOUR CHANGE, DELIBERATE: the old gate was `isUsMarketOpen`, so a dead feed in PRE or AFTER
    // hours raised nothing. Pre-market prices are exactly what #355 is about — an offer rendered as a
    // trade at 08:15 — so a feed that has stopped then matters just as much. `marketSession` treats
    // PRE/OPEN/AFTER as sessions and only CLOSED as quiet.
    if (feed.tone === "down") {
      return { level: "feedStale", message: "Market-data feed stale — prices may be delayed", feed, ownershipViolations, unpricedPositions };
    }
  }
  if (wsActive && !wsConnected) {
    return { level: "wsIssues", message: "Live stream disconnected — reconnecting", feed, ownershipViolations, unpricedPositions };
  }
  return { level: "ok", message: "", feed, ownershipViolations, unpricedPositions };
}
