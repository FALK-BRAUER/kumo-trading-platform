/**
 * Strategy tile (#212) — can I leave this alone, or do I need to act?
 *
 * `research/homescreen.md` argues that is the only question a landing surface answers, and that on a
 * normal day it should be boring. So the loudest thing here is the attention list, and when there is
 * nothing in it the tile says so in one line and stops.
 *
 * All reduction lives in `@/lib/strategy/summary` — this file is layout.
 */
"use client";

import { summarise } from "@/lib/strategy/summary";
import { laneWindowRealized, WINDOW_ORDER, type RealizedPeriodsLike } from "@/lib/strategy/windowDelta";
import type { SessionFrame } from "@/lib/framework/datasource/protocol";
import type { TileProps } from "@/lib/framework/types";
import type { StrategyConfig } from "./schema";
import { useLaneMarketAware } from "@/lib/framework/useLaneMarketAware";
import { marketAwareBadge } from "@/tiles/book/marketAwareBadge";

const STATE_TONE: Record<string, string> = {
  TRADING: "text-emerald-400",
  PAUSED: "text-zinc-400",
  LIQUIDATING: "text-amber-400",
  HALTED: "text-red-400",
};

export function StrategyTile({ data, config }: TileProps<StrategyConfig>) {
  const frame = ((data as { session?: SessionFrame } | undefined)?.session ?? null) as SessionFrame | null;
  const tradesFrame = ((data as { trades?: RealizedPeriodsLike } | undefined)?.trades ?? null);
  const s = summarise(frame);
  const laneId = (config as { strategyId?: string } | undefined)?.strategyId ?? "";
  // #873: the lane's market view, from /strategies — a different fact from the lifecycle chip.
  const laneMarketAware = useLaneMarketAware();

  // No frame is NOT the same as nothing to report — one means the bridge is silent, and showing
  // "nothing needs you" in that case would be a false all-clear.
  if (s.waiting) {
    return <p className="p-3 text-sm text-zinc-500">Waiting for the engine…</p>;
  }

  // A frozen frame is not a calm one. The consumer re-pushes the last payload indefinitely, so
  // without this a dead engine renders as a quiet strategy — with everything below it still true as
  // of whenever it died.
  const staleBanner = s.stale && (
    <p className="text-xs text-red-400">
      Engine has not published for {Math.round((s.ageMs ?? 0) / 60_000)} min — everything below is
      frozen as of then.
    </p>
  );

  // REALIZED PER WINDOW, from the broker sweep — the same buckets the Book rows render, so the two
  // surfaces cannot disagree (#662). Realized-only and labelled so: Δunrealized per lane per window
  // is not derivable (no per-lane equity curves), and the session figure this replaces was a
  // since-boot number wearing a window label (#653). "—" is unknown (pre-sweep frame), not zero.
  const windowStrip = laneId ? (
    <div className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px] text-zinc-500">
      {WINDOW_ORDER.map((w) => {
        const v = laneWindowRealized(tradesFrame, laneId, w);
        return (
          <span key={w}>
            {w === "all" ? "All" : w}{" "}
            <span className={v === null ? "" : v > 0 ? "text-emerald-400" : v < 0 ? "text-red-400" : "text-zinc-300"}>
              {v === null ? "—" : `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(2)}`}
            </span>
          </span>
        );
      })}
      <span className="text-zinc-600">realized, per window</span>
    </div>
  ) : null;

  return (
    <div className="flex flex-col gap-3 p-3 text-sm">
      {staleBanner}
      {windowStrip}
      
      <div className="flex items-baseline justify-between">
        <span className={`font-mono ${STATE_TONE[s.state ?? ""] ?? "text-zinc-400"}`}>
          {s.state ?? "UNKNOWN"}
        </span>
        <span className="text-xs text-zinc-500">{s.reason}</span>
      </div>
      {laneId && (() => {
        const badge = marketAwareBadge(laneMarketAware[laneId]);
        return <p className={`text-xs ${badge.tone}`} title={badge.title}>{badge.text}</p>;
      })()}

      {s.headline && (
        <div>
          <div className="font-mono text-zinc-200">{s.headline}</div>
          <div className="text-xs text-zinc-500">
            {s.session}
            {/* The frame carries the LAST decision, not necessarily today's. Saying so prevents a
                stale rotation from reading as live on a day that blocked or never opened. */}
            {!s.decisionIsToday && <span className="text-amber-400"> · not today&apos;s session</span>}
            {" · "}
            {s.submitted} submitted
          </div>
        </div>
      )}

      {/* Intent vs outcome. The digest once reported "entered 6" on a day that entered zero; a tile
          showing only the summary makes the same mistake in the other direction. */}
      {s.shortfall && (
        <p className="text-xs text-amber-400">
          {/* Deliberately does not say WHICH actions were missed: the frame cannot distinguish a
              policy-skipped entry from a failed exit, and guessing would be worse than not saying. */}
          {s.shortfall.planned} actions planned, {s.shortfall.submitted} submitted —{" "}
          {s.shortfall.planned - s.shortfall.submitted} short
        </p>
      )}

      {s.unprotected.length > 0 && (
        <p className="text-xs text-amber-400">
          peak unknown on {s.unprotected.join(", ")} — peak-relative exits inert
        </p>
      )}

      {s.attention.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {s.attention.map((item) => (
            <li key={item} className="text-xs text-red-400">
              {item}
            </li>
          ))}
        </ul>
      ) : (
        s.allClear && <p className="text-xs text-zinc-500">Nothing needs you.</p>
      )}
    </div>
  );
}
