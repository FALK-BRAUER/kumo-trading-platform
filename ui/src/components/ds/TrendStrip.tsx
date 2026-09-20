import { Sparkline } from "./Sparkline";
import { pctChange, pctLabel, type TrendWindow } from "./trend";

/**
 * TrendStrip (#104 / #114 follow-up) — a row of small labelled sparklines, one per timeframe, each with
 * its own % change. Sits below the Ichimoku sub-line on a Watchlist row (design proposal
 * `docs/design-system/proposal-multi-trend-cloud.md`). Pure/presentational, same shape as `Sparkline`
 * itself: values in, SVG out — no data fetching.
 */
export function TrendStrip({ windows }: { windows: TrendWindow[] }) {
  return (
    // ONE ROW OF SIX, FILLING THE WIDTH IT IS GIVEN. Not a grid, not a wrap, not a fixed pixel size.
    //
    // Three attempts got here. The original single row wanted 265px and overflowed a phone. A 3-wide
    // grid fit in 137px but broke the strip onto two lines — rejected: "not by introducing a line
    // break". Fixed 24px charts fit in one line but were too small to read.
    //
    // The mistake common to all three was picking A NUMBER. Measured in the browser: the left half of
    // a watchlist row is 24px of arrows, the split is 374px, and the right column had been sizing
    // itself to 204px purely because the `TK · KJ · Cld` line above happened to be that wide. There
    // was ~140px of headroom nobody was using, and on a desktop there is far more. So the strip now
    // takes the width it is given and the charts scale into it.
    //
    // HEIGHT IS CAPPED, WIDTH IS NOT. A chart that grows in both directions turns a dense row into a
    // dashboard on a wide screen; `sm:h-[22px]` is the desktop ceiling, 13px the phone.
    <div className="flex w-full gap-1 pt-0.5">
      {windows.map((w) => {
        // An uncovered window is not a flat one. Quoting a % over partial history made the number drift
        // as bars streamed in — the same label showing +6.96% and then +0.26% a minute later, with an
        // unchanged last price — and a "0.00%" on an empty series reads as "did not move".
        const pct = w.covered ? pctChange(w.values) : null;
        const cls = pct == null ? "text-t3" : pct > 0 ? "text-status-bull" : pct < 0 ? "text-status-bear" : "text-t3";
        return (
          // `flex-1 min-w-0` — six equal shares. Without `min-w-0` a flex child refuses to shrink below
          // its content and the label would set the floor again, which is the bug this replaced.
          <div key={w.label} className="flex min-w-0 flex-1 flex-col items-center gap-0.5">
            <span className="text-[8px] tracking-wide text-t3">{w.label}</span>
            <Sparkline values={w.covered ? w.values : []} width={24} height={13} fluid className="h-[13px] sm:h-[22px]" />
            <span
              className={`font-mono text-[8px] leading-none sm:text-[9px] ${cls}`}
              title={w.covered ? `${w.label}: ${pct == null ? "no data" : pct.toFixed(2) + "%"}` : "not enough history loaded"}
            >
              {pctLabel(pct)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export type { TrendWindow };
