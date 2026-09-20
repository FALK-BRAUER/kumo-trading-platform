/**
 * The strategy grid must not look like a complete decomposition when it is not.
 *
 * an operator's live screenshot, 2026-09-15: DELTA NET 1D was $39.55 while the visible strategy headline
 * cells summed to $227.85. The header was broker-period P&L; the cells were lane day moves. Both were
 * useful, but the screen left the gap implicit, so the natural read was "the strategies do not add up".
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { createElement } from "react";
import type { SourceStatus, TileProps } from "@/lib/framework/types";
import type { BookConfig } from "./definition";

const host = vi.hoisted(() => ({
  period: "1D",
  ranges: new Map<string, number>([
    ["AAA.XNAS", 100],
    ["BBB.XNAS", 200],
  ]),
}));

vi.mock("@/lib/framework/store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useCockpitStore: (select: (s: unknown) => unknown) =>
    select({ period: host.period, unit: "$", openDetail: () => {}, openDetailForSymbol: () => {} }),
}));

vi.mock("@/lib/framework/instrument", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useTodayRanges: () => host.ranges,
}));

import { BookTile } from "./BookTile";

const trade = (strategyId: string, instrumentId: string, lastPx: number) => ({
  account_id: "acct",
  client_id: "TEST",
  instrument_id: instrumentId,
  strategy_id: strategyId,
  cycle_id: `${strategyId}:${instrumentId}`,
  manager_id: null,
  state: "HELD",
  side: "LONG",
  quantity: 1,
  is_capital_deployed: true,
  is_engaged: true,
  avg_px_open: lastPx - 1,
  realized_pnl: "0.00 USD",
  last_px: lastPx,
  market_value: lastPx,
  unrealized_pl: 1,
  unrealized_plpc: 0.01,
  leg_count: 1,
  opened_ts: 0,
  closed_ts: null,
  last_event_ts: 0,
  working_orders: [],
  broker_protected: false,
});

function render(): string {
  const props: TileProps<BookConfig> = {
    instanceId: "book-1",
    config: {},
    data: {
      trades: {
        trades: [
          trade("ALPHA-001", "AAA.XNAS", 110), // day headline: +$10
          trade("BETA-002", "BBB.XNAS", 220), // day headline: +$20
        ],
        realized_periods: {
          "1D": { total: 0, by_strategy: {}, unclaimed: 0, unmatched: 0, is_partial: false },
        },
        realized_session: { total: 0, partial_open: 0, is_partial: false },
        status: "ok",
        error: null,
      },
      account: {
        account: {
          equity: 1025,
          cash: 693,
          buying_power: 693,
          multiplier: 1,
          long_market_value: 330,
          last_equity: null,
          unrealized_standing_total: 2,
          unrealized_intraday_total: null,
          currency: "USD",
          ts: 0,
        },
      },
      external_activity: { external: [] },
      equity_curve: {
        curves: {
          "1D": { base_value: 1000, pnl: 25, covered: true, covers_days: 1 },
        },
      },
    },
    status: { trades: "live" as SourceStatus },
    onConfigChange: () => {},
  };
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnMount: false } } });
  qc.setQueryData(["health"], {
    status: "ok",
    subsystems: [{ name: "engine", ok: true }],
    feed_last_tick_ts: Date.now() * 1_000_000,
    reconcile_drift: [],
    protection_divergence: [],
    inert: [],
    ownership_violations: [],
  });
  qc.setQueryData(["pnl-unrealized-base"], {
    by_period: { "1D": null },
    base_date: { "1D": null },
    unreadable: [],
    error: null,
  });
  return renderToString(createElement(QueryClientProvider, { client: qc }, createElement(BookTile, props)))
    .replace(/<!--\s*-->/g, "");
}

describe("strategy grid vs broker net", () => {
  it("renders the residual that makes strategy headlines reconcile to DELTA NET", () => {
    const html = render();
    expect(html).toContain("$25.00");
    expect(html).toContain("$10.00");
    expect(html).toContain("$20.00");
    expect(html).toContain("net residual");
    expect(html).toContain("-$5.00");
  });
});
