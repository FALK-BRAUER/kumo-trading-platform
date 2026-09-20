"use client";

/**
 * EquityTile (#243) — the account's equity curve on Home, with a period selector.
 *
 * Answers a different question from the Book tile sitting beside it, and the difference is the point.
 * Book shows P&L on what is OPEN right now; this shows account equity against where the period
 * started. On 2026-08-12 the account sat at 97,755 against a 100,000 base — down ~2.2% on the month —
 * while Book read positive. Both true. So the labels have to be unambiguous or the pair reads as a
 * contradiction: the period P&L is stated in the header next to the period it belongs to.
 *
 * The P&L shown is the BROKER's `profit_loss` for that window, never a difference derived here from
 * the endpoints — a locally-derived figure would disagree with the statement, which is exactly the
 * parallel-ledger trap #242 and #233 both warn about.
 *
 * Read-only.
 */
import { useState } from "react";
import { EquityCurve, type EquityPoint } from "@/components/ds/EquityCurve";
import { TileState } from "@/components/board/TileState";
import { useCockpitStore } from "@/lib/framework/store";
import type { TileProps } from "@/lib/framework/types";
import { headlineEquity } from "./headline";
import { periodNet } from "@/tiles/book/periodNet";
import { deriveSessionCurve } from "./sessionCurve";
import type { AccountDTO } from "@/lib/api/types";
import type { EquityConfig } from "./definition";

interface Curve {
  points: EquityPoint[];
  base_value: number;
  pnl: number;
  timeframe: string;
}

function fmtUsd(n: number): string {
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

export function EquityTile({ data, status: srcStatus }: TileProps<EquityConfig>) {
  // The period is GLOBAL now (#233 follow-up). It was local here, so the chart answered for one window
  // while every figure above it answered for another and nothing said so — which is how "+$2,841 this
  // 1W" sat directly under a NET of $1,061 with no way to reconcile them.
  const period = useCockpitStore((s) => s.period);
  const equity = data.equity_curve as
    { curves?: Record<string, Curve>; unavailable?: string } | undefined;
  const curves = equity?.curves ?? {};
  // WHY THIS STACK HAS NO HISTORY, when the engine knows (#559). `broker.equity_curve` has exactly one
  // publisher — the Alpaca exec client — so on an IBKR stack nothing ever writes the topic. The tile
  // then drew "No account history yet" over an account holding $1,000,000 with months of history:
  // true of a fresh Alpaca account, false here. The engine states the absence after a boot grace and
  // says why; the wording comes from there rather than being invented a second time here.
  const unavailable = equity?.unavailable;
  // NO 1D SERIES IS EVER FETCHED (#345 item 4). `_EQUITY_PERIODS` is 1W/1H, 1M/1D, 3M/1D, all/1D — so
  // the DEFAULT tab rendered "not enough history yet" directly under a header quoting that day's move.
  // The points exist: the 1W series is hourly, so today's session is a suffix of it. Derived rather than
  // fetched — a fifth broker call would be a second source for a fact the first one already carries.
  const curve = period === "1D" ? (curves["1D"] ?? deriveSessionCurve(curves["1W"], Date.now())) : curves[period];
  const points = curve?.points ?? [];
  const curveLast = points.length ? points[points.length - 1].equity : null;
  // The headline is CURRENT equity, so it must not change with the window (#336). It used to be the
  // selected curve's last point, which read $100,679 at 1W and $100,116 at 1M while LIQUIDATION beside
  // it said $100,658.95. Broker equity is the same field LIQUIDATION renders — one fact, one number.
  const account = (data.account as { account?: AccountDTO | null } | undefined)?.account ?? undefined;
  const last = headlineEquity(account, curveLast);
  // THE DELTA IS THE SAME FACT `BOOK` CALLS NET, so it comes from the same function (#343). It used to
  // be `curve.pnl`, which the broker measures to the last point of its portfolio history — the previous
  // session's close. Live 2026-08-18 premarket that put "+$1,556 this 1M" beside a headline of $100,232
  // over a chart starting at $99,291: the header contradicted its own chart by exactly the day's loss.
  // Reading `periodNet` makes the header and BOOK's NET one derivation rather than two that can drift.
  const pnl = periodNet(data.equity_curve as Parameters<typeof periodNet>[0], period, account);
  const pnlCls = pnl == null ? "text-t3" : pnl > 0 ? "text-status-bull" : pnl < 0 ? "text-status-bear" : "text-t3";

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-t2">Equity</h2>
          {last != null && <span className="font-mono text-lg font-semibold text-t1">{fmtUsd(last)}</span>}
          {pnl != null && (
            <span className={`font-mono text-xs ${pnlCls}`}>
              {pnl >= 0 ? "+" : ""}
              {fmtUsd(pnl)} <span className="text-t3">this {period === "all" ? "account" : period}</span>
            </span>
          )}
        </div>
        {/* NO period selector here (#336). The global one lives in Board.tsx and already drives this
            tile through the shared store; mounting a second control for the same state put two selectors
            on one screen, which reads as two independent scopes when it is one. */}
      </div>

      <TileState
        status={srcStatus.equity_curve}
        isEmpty={Object.keys(curves).length === 0}
        // NOT a zero line. An empty curve means the broker history has not arrived, which is different
        // from an account that has done nothing — drawing a flat zero would assert the second.
        //
        // And "yet" is a claim too: it says the history is coming. When the engine has told us this
        // stack cannot produce one at all, say THAT instead of implying a wait that will never end.
        emptyLabel={unavailable ?? "No account history yet"}
      >
        <EquityCurve points={points} />
        {curve && (
          <div className="mt-1 font-mono text-[10px] text-t3">
            {points.length} points · {curve.timeframe} · from {fmtUsd(curve.base_value)} ·{" "}
            {/* Disclosed, because it changes how the slope reads: points are EVENLY spaced, so a
                weekend or holiday takes no horizontal room. Unstated, a gap looks like time in which
                nothing moved. (codex review, Medium.) */}
            <span title="Points are evenly spaced; closed sessions take no width, so horizontal distance is samples, not elapsed time.">
              evenly spaced
            </span>
          </div>
        )}
      </TileState>
    </div>
  );
}
