"use client";

/**
 * Market tile (#351) — which rotation the market is in.
 *
 * The cockpit answers "what do I hold and how is it doing" and never "where is money moving". Rotation
 * gets read by eye off TradingView, one chart at a time, usually after the fact — on 2026-08-18 energy
 * was bid on Hormuz while semis took −3% and nothing here said so.
 *
 * Each row is one axis: a RATIO of two ETFs, graded by the same weekly-governs / daily-times Ichimoku
 * stack that grades single names. None of that maths is here. `fintrack:tools/rotation_read.py` emits
 * the contract and the api serves it; the tile translates the gate into plain words exactly once, in
 * `rotationRead.ts`, and renders.
 *
 * WHAT THIS TILE REFUSES TO DO. It does not rank by percentage. A drift estimate carries a 95% interval
 * that usually straddles zero, so ordering axes by the estimate manufactures conviction the data does
 * not support. The interval is always on screen beside the number, and an axis with no interval shows
 * no number at all — degrading to the most confident figure exactly when the data is weakest is the
 * failure this tile exists to avoid.
 *
 * COLOUR IS NEVER ALONE. The Read word ships at every breakpoint, per the design-system note in
 * globals.css. A colourblind operator and a greyscale screenshot both read the same thing.
 */
import type { RotationAxis, RotationResponse } from "@/lib/api/client";
import { useState } from "react";
import { useCockpitStore } from "@/lib/framework/store";
import type { TileProps } from "@/lib/framework/types";
import type { MarketConfig } from "./definition";
import {
  driftLabel,
  gateOf,
  isConfirmed,
  railDomain,
  railMarks,
  leaderLabel,
  legTone,
  readVerdict,
  sortAxes,
  type Gate,
  type RailMarks,
  type Read,
  type SortMode,
} from "./rotationRead";
import { MomentumMap } from "./MomentumMap";
import { readAge } from "./readAge";

/**
 * The cockpit's selector says `all`; the rotation payload's longest window is `1Y`.
 *
 * Mapped rather than given its own control. #336 established that two selectors for one piece of state
 * read as two independent scopes when it is one — the Equity tile lost its private selector for exactly
 * this reason. A rotation has no inception-to-date meaning anyway, so `all` resolving to the longest
 * available window is the honest reading of the same intent.
 */
function windowKey(period: string): string {
  return period === "all" ? "1Y" : period;
}

const SORTS: Array<[SortMode, string]> = [
  ["trend", "trending"],
  ["drift", "drift"],
  ["travel", "travel"],
  ["pair", "a\u2013z"],
];

/** Semantic colour for the read, always paired with the word itself — never carrying meaning alone. */
/** One Gate -> class map. The rail had its own copy inline; two of them would drift. */
function toneClass(gate: Gate): string {
  return gate === "bull" ? "text-status-bull" : gate === "bear" ? "text-status-bear" : "text-t3";
}

function readClass(read: Read, winner: "num" | "den" | null): string {
  if (read === "No trend") return "text-t3";
  if (winner === "den") return "text-status-bear";
  return "text-status-bull";
}

/**
 * The rail — five marks, all in the axis's own colour (#351).
 *
 * band  : the 95% interval, wide and translucent
 * core  : ±1 standard error inside it, denser — an interval drawn as one flat band reads as uniform
 *         certainty when most of the probability sits in its middle third
 * ghost : small hollow dot at last window's position
 * move  : bar + arrowhead from ghost to now, so the DIRECTION of travel is visible without reading
 * dot   : big dot at the current position — FILLED when the gate is confirmed, outlined when it is only
 *         leaning. Fill is a fact, not a flourish.
 *
 * Pure CSS percentage positioning: no SVG, no layout JS, reflows with the column at every breakpoint.
 */
function Rail({ marks, gate, confirmed }: { marks: RailMarks; gate: Gate; confirmed: boolean }) {
  // `currentColor` carries the axis colour to every child, so the family is set once here rather than
  // threaded through six className strings that could drift apart.
  const tone =
    gate === "bull" ? "text-status-bull" : gate === "bear" ? "text-status-bear" : "text-status-watch";
  const pct = (v: number) => `${v}%`;
  return (
    <div className={`relative h-5 w-full min-w-[90px] ${tone}`} aria-hidden>
      <div className="absolute left-1/2 top-0.5 bottom-0.5 z-[2] w-px -translate-x-1/2 bg-ds-line" />
      <div
        className="absolute top-1.5 z-0 h-2 rounded-full bg-current opacity-[0.18]"
        style={{ left: pct(marks.lo), width: pct(marks.hi - marks.lo) }}
      />
      <div
        className="absolute top-1.5 z-[1] h-2 rounded-full bg-current opacity-[0.38]"
        style={{ left: pct(marks.core1), width: pct(marks.core2 - marks.core1) }}
      />
      {marks.moveFrom !== null && marks.moveWidth !== null && (
        <>
          <div
            className="absolute top-[9.3px] z-[3] h-[1.4px] bg-current"
            style={{ left: pct(marks.moveFrom + (marks.moveRight ? 1.1 : 0)), width: pct(Math.max(marks.moveWidth - 2.2, 0)) }}
          />
          <div
            className="absolute top-[5.5px] z-[3] h-0 w-0 border-y-[4.5px] border-y-transparent"
            style={
              marks.moveRight
                ? { left: pct(marks.moveFrom + marks.moveWidth - 0.2), borderLeft: "6px solid currentColor" }
                : { left: pct(marks.moveFrom - 0.2), borderRight: "6px solid currentColor", transform: "translateX(-6px)" }
            }
          />
        </>
      )}
      {marks.prev !== null && (
        <span
          className="absolute top-1.5 z-[3] h-2 w-2 -translate-x-1/2 rounded-full border-[1.5px] border-current bg-ds-bg"
          style={{ left: pct(marks.prev) }}
        />
      )}
      <span
        className={`absolute top-[4.5px] z-[4] h-[11px] w-[11px] -translate-x-1/2 rounded-full ring-2 ring-ds-bg ${
          confirmed ? "bg-current" : "border-2 border-current bg-ds-bg"
        }`}
        style={{ left: pct(marks.est) }}
      />
    </div>
  );
}

function AxisRow({
  axis,
  period,
  domain,
}: {
  axis: RotationAxis;
  period: string;
  domain: number;
}) {
  const win = axis.win?.[windowKey(period)] ?? null;
  const { read, winner } = readVerdict(axis.verdict);
  const drift = driftLabel(win);
  const marks = railMarks(win, domain);
  const gate = gateOf(axis.verdict);

  return (
    <div className="border-b border-ds-line px-3 py-2 last:border-b-0">
      <div className="flex items-baseline justify-between gap-2">
        <div className="flex min-w-0 items-baseline gap-2">
          {/* THE PAIR ITSELF CARRIES THE DIRECTION (#389). It rendered as one neutral string, so the
              only thing on the row that said which leg was winning was the colour of a word that read
              "Trending" either way. Colouring the legs is the operator's suggestion and makes the ratio
              readable without decoding a verdict: winner green, loser red, both grey when there is no
              gate. The slash stays neutral — it is punctuation, not a leg. */}
          <span className="shrink-0 font-mono text-xs font-semibold">
            <span className={toneClass(legTone(axis.verdict, "num"))}>{axis.num}</span>
            <span className="text-t3">/</span>
            <span className={toneClass(legTone(axis.verdict, "den"))}>{axis.den}</span>
          </span>
          {/* THE DESCRIPTION SHIPS AT EVERY WIDTH. It used to be `hidden sm:inline`, so on a phone every
              row read as a bare ticker pair — "I would love to have a full description not only
              IWM/SPY". `label` names the axis, `meaning` says why anyone cares; the second is the one
              that turns a ratio into a read, so it gets the row underneath rather than a tooltip. */}
          <span className="truncate font-mono text-[10px] text-t3">{axis.label}</span>
        </div>
        {/* NAMES THE LEADER (#389). `read` alone was "Trending" for both ON and OFF, so colour carried
            the whole direction — which #351's own acceptance forbids ("colour never alone, the Read word
            ships with it at every breakpoint") and which is unreadable in greyscale. A ratio has no
            direction of its own: XLE/SPY rising is energy beating the market, the same path falling is
            the market beating energy. `leaderLabel` keeps the path state as a modifier. */}
        <span className={`shrink-0 font-mono text-[11px] font-semibold ${readClass(read, winner)}`}>
          {leaderLabel(axis.verdict, axis.num, axis.den)}
        </span>
      </div>
      {axis.meaning && (
        <div className="mt-0.5 truncate text-[10px] leading-tight text-t3" title={axis.meaning}>
          {axis.meaning}
        </div>
      )}
      <div className="mt-1 flex items-center gap-3">
        {marks ? (
          <Rail marks={marks} gate={gate} confirmed={isConfirmed(axis.verdict)} />
        ) : (
          <span className="flex-1" />
        )}
        {/* The interval travels with the estimate or neither is shown. A bare percentage here is the
            overstatement the whole tile is built to avoid. */}
        <span className="shrink-0 font-mono text-[10px] text-t2">{drift ?? "—"}</span>
      </div>
    </div>
  );
}

export function MarketTile({ config, data, status }: TileProps<MarketConfig>) {
  const period = useCockpitStore((s) => s.period);
  const panel = config?.panel ?? "both";
  const [sort, setSort] = useState<SortMode>("trend");

  // BOUND BY NAME, NOT FETCHED. The container resolves the `rotation` source and hands it down; this
  // component does not know the endpoint exists. That is the framework's core rule (`ui-framework-spec`
  // §1d: "Tiles never fetch directly — the container owns all data wiring"), and the first cut broke it.
  //
  // It also DELETES a bug rather than moving it. Hand-rolling the fetch meant hand-rolling "is this
  // absent or merely slow", and that took four attempts: `isLoading` dropped between retries so a tile
  // retrying against a 404 announced an empty market, and `isError` never surfaced at all, leaving it on
  // "Loading…" forever. `mapToTileStatus` is the ONE place that mapping is supposed to live, and it
  // already distinguishes loading / live / stale / error — including "errored but holding a last value",
  // which the hand-rolled version had no way to express.
  const payload = data.rotation as RotationResponse | undefined;
  const sourceStatus = status.rotation;
  const axes = payload?.axes ?? [];
  const age = readAge(payload?.generated, Date.now());
  const key = windowKey(period);
  // ONE DOMAIN FOR EVERY RAIL. Scaling each row by its own interval (the first cut) made two rails with
  // wildly different magnitudes look identical, so the column could not be read AS a column.
  const domain = railDomain(axes.map((a) => a.win?.[key]));
  const shown = sortAxes(axes, sort, (a) => a.win?.[key]);

  // ABSENCE IS NOT AN EMPTY MARKET. Two different absences, kept apart: the api answering with its own
  // `error` (the payload was never generated), and the api not answering at all. Both must say so rather
  // than render as a calm tape — the same rule an unpriced symbol follows (#356) and NET follows for an
  // unknown window (#343).
  const absent =
    payload?.error ?? (sourceStatus === "error" ? "no response from the api" : null);

  return (
    <div>
      {/* NO PeriodSelector here. The Board renders the global one and this tile reads the same store —
          mounting its own put two identical controls on screen, which #336 established reads as two
          independent scopes when it is one. Caught in the browser, not by a test: a second selector is
          correct in isolation and wrong only in place. */}
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-t2">Market</h2>
          <span className="font-mono text-[10px] text-t3">
            {axes.length > 0 ? `${axes.length} rotations` : ""}
          </span>
          {/* AGE WHERE THE READ IS (#351 follow-up). This was a raw locale timestamp below all 25 rows;
              the operator asked "premarket or yesterday?" of a payload generated 14:05 ET the previous day and
              nothing on screen answered it. `· source stale` beside it keys on the FETCH, which is always
              fresh — a staleness flag that cannot see the staleness in front of it reads as an all-clear. */}
          {age && (
            <span
              className={`font-mono text-[10px] ${
                age.tone === "stale" ? "text-status-bear" : age.tone === "aging" ? "text-status-watch" : "text-t3"
              }`}
              title={`Rotation read generated ${payload?.generated ? new Date(payload.generated).toLocaleString() : "unknown"}. A rotation is a DAILY read: "stale" means a session has closed since it was computed, so it never saw the session it describes.`}
            >
              {age.tone === "stale" ? `stale · ${age.label}` : age.label}
            </span>
          )}
        </div>
      </div>

      {/* ABSENCE IS NOT AN EMPTY MARKET. A payload nobody generated must say so, not render as a calm
          tape — the same rule an unpriced symbol follows (#356) and NET follows for an unknown window
          (#343). The api already distinguishes the two; the tile must not flatten them back together. */}
      {absent ? (
        <div className="rounded-lg border border-dashed border-ds-line px-3 py-6 text-center font-mono text-[11px] text-t3">
          No rotation read available — {absent}
        </div>
      ) : sourceStatus === "loading" ? (
        // NOTHING TO SAY ABOUT THE MARKET UNTIL SOMETHING ANSWERS. `loading` here comes from
        // `mapToTileStatus`, which reports it only while there is no value at all — a retry that still
        // holds a previous payload reports `stale` and keeps rendering it, which is what an operator
        // wants from a once-a-day read.
        <div className="px-3 py-6 text-center font-mono text-[11px] text-t3">Loading…</div>
      ) : axes.length === 0 ? (
        <div className="px-3 py-6 text-center font-mono text-[11px] text-t3">No rotations to show</div>
      ) : (
        <div className="space-y-3">
          {/* THE MAP FIRST. The rows answer "what is this axis doing"; the scatter answers "where is
              money moving", which is the question the tile was asked for and the one an operator has
              before they know which row to look at. */}
          {panel !== "rows" && (
            <MomentumMap axes={axes} windowKey={windowKey(period)} height={config?.mapHeight ?? 224} />
          )}
          {panel !== "map" && (
            <>
              {/* SORTING IS THE OPERATOR'S, WITH A DEFENSIBLE DEFAULT. #351 argues that ranking by
                  percentage manufactures conviction the data does not support, and that the
                  discriminating signal is persistence — the gate and ADX. So `Trending` is the default
                  order, and drift is available where it has been ASKED for rather than handed over as
                  though it were the ranking. */}
              <div className="flex flex-wrap items-center gap-1 px-1">
                <span className="mr-1 font-mono text-[10px] text-t3">sort</span>
                {SORTS.map(([mode, label]) => (
                  <button
                    key={mode}
                    type="button"
                    onClick={() => setSort(mode)}
                    aria-pressed={sort === mode}
                    className={`rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors ${
                      sort === mode
                        ? "bg-ds-line text-t1"
                        : "text-t3 hover:text-t2"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="overflow-hidden rounded-md border border-ds-line">
                {shown.map((a) => (
                  <AxisRow key={a.pair} axis={a} period={period} domain={domain} />
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {payload?.generated && (
        <div className="mt-1 px-1 font-mono text-[10px] text-t3">
          {/* Age, stated. A rotation read is daily; an operator needs to know whether they are looking
              at today's or Friday's, and a tile that hides it lets a dead refresh pass for a quiet tape. */}
          read generated {new Date(payload.generated).toLocaleString()}
          {sourceStatus === "stale" ? " · source stale" : ""}
        </div>
      )}
    </div>
  );
}
