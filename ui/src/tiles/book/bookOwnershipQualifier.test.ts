/**
 * The held count is QUALIFIED when ownership is disputed (#437), proven by rendering the tile.
 *
 * WHY THIS FILE EXISTS. Review's ship condition named two mutants; one was fixed by extracting
 * `classifyHealth`, and this one survived: deleting BookTile's `{ownershipViolations.length > 0 &&
 * (...)}` span left all 850 tests green. `ownership.test.ts` covers the message formatter — the
 * unit, not the seam — so nothing proved the tile renders anything at all.
 *
 * That is the same defect the whole ticket is about, on its third surface. #437 was a detector
 * nothing called. Its fix was an announcer nothing proved was called. Its UI half was a banner
 * nothing proved was raised, and this qualifier.
 *
 * IT ASSERTS THE COUNT, NOT THE WORD. "disputed appears" passes on a heading that happens to contain
 * it, and is defeated by a copy change that leaves the wiring broken. The number must TRACK the
 * violations, so what is pinned is the connection rather than the phrasing.
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

/** A violation shaped as `/health` serves it: the lane, the instrument, and a NEGATIVE quantity. */
const v = (strategy_id: string, instrument_id: string, signed_qty: number) => ({
  strategy_id,
  instrument_id,
  signed_qty,
});

function renderWith(ownership_violations: ReturnType<typeof v>[] | null): string {
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
  // Seeded rather than fetched. `useHealth` polls `GET /health`; server-side rendering has no
  // network, and a suspended query would make these assertions race a layer that does not exist.
  qc.setQueryData(["health"], {
    status: "ok",
    subsystems: [{ name: "engine", ok: true }],
    feed_last_tick_ts: Date.now() * 1_000_000,
    reconcile_drift: [],
    protection_divergence: [],
    inert: [],
    ownership_violations,
  });
  const html = renderToString(
    createElement(QueryClientProvider, { client: qc }, createElement(BookTile, props)),
  );
  // React's server renderer splits adjacent text nodes with `<!-- -->` markers, so the rendered
  // "1 disputed" arrives as `1<!-- --> disputed`. Stripping them asserts on what a READER sees
  // rather than on React's hydration bookkeeping — otherwise the test fails on a framework detail
  // while the wiring it exists to pin is perfectly correct, which is a test that teaches the next
  // person the wrong lesson.
  return html.replace(/<!--\s*-->/g, "");
}

describe("the held count is qualified when ownership is disputed", () => {
  it("renders NO qualifier when nothing is disputed", () => {
    const html = renderWith([]);
    expect(html).not.toMatch(/disputed/i);
  });

  it("renders the COUNT, and the count TRACKS the violations", () => {
    const one = renderWith([v("MOMENTUM-002", "WHD.XNYS", -28)]);
    const three = renderWith([
      v("MOMENTUM-002", "WHD.XNYS", -28),
      v("EXTERNAL", "TOST.XNYS", -74),
      v("EXTERNAL", "VEEV.XNYS", -10),
    ]);

    // FIXTURE PROPERTY FIRST: the two renders must actually differ, or a static string satisfies
    // both assertions and the wiring is unproven — which is the bug under test.
    expect(one).not.toEqual(three);

    expect(one).toMatch(/1\s*disputed/i);
    expect(three).toMatch(/3\s*disputed/i);
    // And the wrong count must NOT appear, so a hardcoded "1 disputed" cannot pass the pair.
    expect(three).not.toMatch(/1\s*disputed/i);
  });

  it("renders no qualifier when the check could not READ the book", () => {
    // `/health` serves null — distinct from `[]` — when `signed_qty_of` could not understand the
    // position shape. A count rendered from unknown would be a claim; absence here is correct, and
    // the banner is what says the check is broken.
    expect(renderWith(null)).not.toMatch(/disputed/i);
  });
});
