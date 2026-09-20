/**
 * The window delta REACHES A RENDERED CELL (#699).
 *
 * WHY THIS FILE EXISTS. Every piece below it is unit-tested — `windowDelta`'s three states,
 * `cellHeadline` carrying rather than summing, the endpoint's payload. None of that proves the tile
 * FETCHES the base, threads it to the right lane, and renders it.
 *
 * That gap is the defect this entire chain kept producing: a detector nothing called, an announcer
 * nothing proved was called, a banner nothing proved was raised, a qualifier nothing proved
 * rendered. Four surfaces, one shape. This is the fifth and it gets a seam test on the way in rather
 * than after review finds it.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { createElement } from "react";
import type { SourceStatus, TileProps } from "@/lib/framework/types";
import type { BookConfig } from "./definition";
import { FRAME } from "@/tiles/managed-portfolio/liveFrame.fixture";

const host = vi.hoisted(() => ({ period: "1M", ranges: new Map<string, number>() }));

vi.mock("@/lib/framework/store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useCockpitStore: (select: (s: unknown) => unknown) =>
    select({ period: host.period, openDetail: () => {}, openDetailForSymbol: () => {} }),
}));

vi.mock("@/lib/framework/instrument", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useTodayRanges: () => host.ranges,
}));

import { BookTile } from "./BookTile";

function render(base: unknown): string {
  const props: TileProps<BookConfig> = {
    instanceId: "book-1",
    config: {},
    data: {
      trades: FRAME,
      account: { account: null },
      external_activity: { external: [] },
      equity_curve: null,
    },
    status: { trades: "live" as SourceStatus },
    onConfigChange: () => {},
  };
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnMount: false } },
  });
  qc.setQueryData(["health"], {
    status: "ok", subsystems: [{ name: "engine", ok: true }],
    feed_last_tick_ts: Date.now() * 1_000_000,
    reconcile_drift: [], protection_divergence: [], inert: [], ownership_violations: [],
  });
  // THE BASE, SEEDED — the tile fetches it through react-query, and server-side rendering has no
  // network. Seeding is what makes this a seam test rather than a mock of the tile's own logic.
  qc.setQueryData(["pnl-unrealized-base"], base);
  return renderToString(
    createElement(QueryClientProvider, { client: qc }, createElement(BookTile, props)),
  ).replace(/<!--\s*-->/g, "");
}

describe("the fetched base reaches the cell", () => {
  it("renders a DIFFERENT cell when a base exists than when it does not", () => {
    // FIXTURE PROPERTY FIRST, AND IT IS THE WHOLE TEST. If these two renders were identical the
    // base would be fetched, threaded and ignored — which is precisely the failure mode this file
    // exists for, and it would pass every unit test underneath it.
    const withBase = render({
      by_period: { "1M": { "MOMENTUM-002": -100 } }, base_date: { "1M": "2026-07-31" },
      unreadable: [], error: null,
    });
    const without = render({
      by_period: { "1M": null }, base_date: { "1M": null }, unreadable: [], error: null,
    });
    expect(withBase).not.toEqual(without);
  });

  it("does not fabricate a delta before the fetch has answered", () => {
    // `undefined` query data must not read as "captured and flat", which `windowDelta` treats as a
    // KNOWN ZERO. A not-yet-answered fetch rendering as "the mark did not move" would be a
    // confident wrong number on first paint, every paint.
    const pending = render(undefined);
    const flat = render({
      by_period: { "1M": {} }, base_date: { "1M": "2026-07-31" }, unreadable: [], error: null,
    });
    expect(pending).not.toEqual(flat);
  });
});
