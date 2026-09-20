/**
 * #1099 — the Portfolio chip row lists every lane REGISTERED ON THIS NODE, including one that holds
 * nothing.
 *
 * Measured on an Alpaca paper instance 2026-09-17: QC345-003 was armed, registered (`/strategies` row, sleeve 20,000,
 * `next_rebalance 2026-10-01`) and holding 0 after protection closed its whole book 09-10..09-14.
 * The Portfolio tab showed no QC345 chip at all, because the chips were
 *
 *     const strategies = Array.from(new Set(groups.map((g) => g.strategyId))).sort();
 *
 * — derived from ENGAGED CYCLES only. A lane with no cycle has no chip, so the one screen the
 * operator reads could not show that a lane had gone flat. The chips must come from the registry
 * (`/strategies`, which the tile already fetches for its sleeves) unioned with the cycles, and the
 * flat lane must read `QC345 0`.
 *
 * WHICH REGISTRY ROWS ARE "REGISTERED". `/strategies` lists every lane the registry KNOWS, not every
 * lane this node BUILT: on paper 2026-09-18 it carried seven rows, of which CRSISHORT-006 was not
 * enabled on the instance (`arm.state: "unknown"`, sleeve 0) and MANUAL-001 never arms (`cadence:
 * "manual"`, `arm.state: "unknown"`) but is always live. A `CRSISHORT 0` chip would claim a lane this
 * node does not run. So a row earns a chip when `arm.state` is not `"unknown"` OR the cadence is
 * `manual` — the same three-valued arm row #997 made the bridge publish. Pinned below with the real
 * payload shape (the 09-17 payload; the PREDICATE decides, not the list — a lane enabled tomorrow
 * flips in by its own arm state).
 *
 * AND ONLY WHEN THE FRAME WAS READ. Every row reads `unknown` when the api could not read the
 * engine frame (stale bridge; the new-api/old-engine seconds at every deploy). `/strategies` says
 * so top-level (`engine_frame`), and the chips fall back to the cycles' lanes on anything but `"ok"`
 * — otherwise the fix would re-create the symptom on the tick a deploy lands (cross-review).
 *
 * DRIVEN THROUGH THE RENDERED TILE, as `ManagedPortfolioTile.short.test.ts` does: the chip row is
 * inline JSX, so a test of a helper would assert nothing about what renders. `useStrategyRows` is
 * stubbed at the module boundary with the row shape `/strategies` returns, through a mutable holder
 * so the loading / failed states are expressible (cross-review, h2ho0jjf, 2026-09-18).
 *
 * SEEN RED on origin/main b311d29 (2026-09-18), 4 of 12: the sorted-union case, the no-cycles case,
 * the one-lane boundary and the EXTERNAL case — the chips read `["All 1", "MOMENTUM 1",
 * "Unprotected 1"]` with a held MOMENTUM cycle and `[]` with none. GREEN on main by design: the
 * `All`/`Unprotected` row-count pin, the ONE-`MANUAL`-chip pin (main renders no registry chips, so it
 * cannot double one — the case guards the naive union, not today's code), and the two "today's
 * behaviour" fallbacks.
 */
import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { TradeDTO } from "@/lib/api/types";
import { strategyLabel } from "@/lib/framework/position";
import type { StrategyRow, StrategyRows } from "@/lib/framework/useStrategyRows";

/** `/strategies` on paper, 2026-09-18 00:05 SGT — the seven rows, reduced to the fields the chips
 *  read. `arm.state` and `cadence` are verbatim; `target` too. */
const PAPER_REGISTRY: StrategyRow[] = [
  { strategy_id: "MANUAL-001", target: 0, cadence: "manual", arm: { state: "unknown" } },
  { strategy_id: "MOMENTUM-002", target: 20_000, cadence: "daily", arm: { state: "armed" } },
  { strategy_id: "BCTROT-004", target: 20_000, cadence: "daily", arm: { state: "armed" } },
  { strategy_id: "TECHIVOL-005", target: 20_000, cadence: "daily", arm: { state: "armed" } },
  { strategy_id: "QC345-003", target: 20_000, cadence: "monthly", next_rebalance: "2026-10-01", arm: { state: "armed" } },
  { strategy_id: "CRSISHORT-006", target: 0, cadence: "daily", arm: { state: "unknown" } },
  { strategy_id: "SMHGLD-007", target: 20_000, cadence: "daily", arm: { state: "armed" } },
];
/** The rows that run on this node: every armed row plus the manual lane. NOT CRSISHORT-006. */
const ON_THIS_NODE = ["MANUAL-001", "MOMENTUM-002", "BCTROT-004", "TECHIVOL-005", "QC345-003", "SMHGLD-007"];

const LIVE: StrategyRows = { rows: PAPER_REGISTRY, failed: false, frame: "ok" };
const registry: { current: StrategyRows } = { current: LIVE };

vi.mock("@/lib/framework/useStrategyRows", () => ({
  useStrategyRows: () => registry.current,
}));

vi.mock("@/lib/framework/instrument", async (importActual) => {
  const actual = await (importActual as () => Promise<Record<string, unknown>>)();
  return {
    ...actual,
    useTodayRanges: () => new Map<string, number>(),
    useBars: () => [],
    useInstrument: () => ({
      bars: [],
      price: 110,
      quote: null,
      vwap: null,
      todayRange: null,
      fundamentals: null,
      fills: [],
      status: "live",
    }),
  };
});

const { ManagedPortfolioTile } = await import("./ManagedPortfolioTile");

/** One engaged cycle, unprotected (no working orders), on the given lane and symbol. */
const cycle = (strategy_id: string, instrument_id = "AAPL.XNAS"): TradeDTO =>
  ({
    account_id: "DU1",
    client_id: "ALPACA",
    instrument_id,
    strategy_id,
    cycle_id: `${strategy_id}:${instrument_id}`,
    state: "HELD",
    side: "LONG",
    quantity: 10,
    is_capital_deployed: true,
    is_engaged: true,
    avg_px_open: 100,
    realized_pnl: "0.00 USD",
    last_px: 110,
    market_value: 1100,
    unrealized_pl: 100,
    leg_count: 1,
    opened_ts: 0,
    last_event_ts: 0,
    working_orders: [],
  }) as TradeDTO;

const HELD = cycle("MOMENTUM-002");

function renderRaw(trades: TradeDTO[]): string {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: qc } as never,
      createElement(ManagedPortfolioTile as never, {
        instanceId: "i",
        config: {},
        onConfigChange: () => {},
        data: {
          trades: { trades },
          account: { account: { equity: 100_000 } },
          managers: { managers: [] },
          external_activity: { external: [] },
        },
        status: { trades: "live", account: "live", managers: "live", external_activity: "live" },
      } as never),
    ),
  );
}

/** The chip row's buttons, in order, as `LABEL N` — read from the CHIP ROW's own markup, not from
 *  the tag-stripped page. A `QC345 0` printed anywhere else (a book cell, a header) must not count
 *  (cross-review item 4). `[]` when the row is not rendered. */
function chips(html: string): string[] {
  const row = html.match(/<div class="mb-2 flex flex-wrap gap-1">(.*?)<\/div>/);
  if (!row) return [];
  return [...row[1].matchAll(/<button[^>]*>([^<]*)<\/button>/g)].map((m) => m[1].trim());
}

/** What the chip row must read for `trades` against the registry rows that run on this node. */
function expected(trades: TradeDTO[], onNode: string[]): string[] {
  const labels = new Set([...onNode, ...trades.map((t) => t.strategy_id)].map(strategyLabel));
  const count = (label: string) => trades.filter((t) => strategyLabel(t.strategy_id) === label).length;
  const groups = new Set(trades.map((t) => t.instrument_id)).size;
  return [`All ${groups}`, ...[...labels].sort().map((l) => `${l} ${count(l)}`), `Unprotected ${groups}`];
}

describe("the fixture can express the bug", () => {
  it("the registry names lanes that NO cycle carries — those chips can only come from the registry", () => {
    const cycleLanes = new Set([HELD].map((t) => t.strategy_id));
    const flat = ON_THIS_NODE.filter((id) => !cycleLanes.has(id));
    expect(flat.length).toBeGreaterThanOrEqual(2);
    expect(flat).toContain("QC345-003");
    expect(cycleLanes.has("MOMENTUM-002")).toBe(true);
  });

  it("the registry carries a row this node does NOT run, so the predicate has something to exclude", () => {
    const off = PAPER_REGISTRY.filter((r) => !ON_THIS_NODE.includes(r.strategy_id ?? ""));
    expect(off.map((r) => r.strategy_id)).toEqual(["CRSISHORT-006"]);
    expect(off[0].arm?.state).toBe("unknown");
    // ...and the manual lane shares that arm state, so `arm.state !== "unknown"` ALONE would drop it.
    expect(PAPER_REGISTRY.find((r) => r.strategy_id === "MANUAL-001")?.arm?.state).toBe("unknown");
  });

  it("the rendered chip text is `strategyLabel(id) N` — the expectation is derived through the same function", () => {
    expect(strategyLabel("QC345-003")).toBe("QC345");
    expect(strategyLabel("BridgeStrategy-000")).toBe(strategyLabel("MANUAL-001"));
  });

  it("the matcher reads the CHIP ROW: every chip the cycles already produce, and nothing when the row is absent", () => {
    registry.current = { rows: undefined, failed: false, frame: undefined };
    try {
      expect(chips(renderRaw([HELD]))).toEqual(["All 1", "MOMENTUM 1", "Unprotected 1"]);
      expect(chips(renderRaw([]))).toEqual([]);
    } finally {
      registry.current = LIVE;
    }
  });
});

describe("#1099 — every lane registered on this node keeps its chip", () => {
  it("renders `<LABEL> 0` for each flat lane beside the lane the cycles name, in sorted order, and nothing else", () => {
    // MANUAL 0, BCTROT 0, QC345 0, SMHGLD 0, TECHIVOL 0 and MOMENTUM 1 — and NO CRSISHORT chip.
    const got = chips(renderRaw([HELD]));
    expect(got).toEqual(expected([HELD], ON_THIS_NODE));
    expect(got).toContain("QC345 0");
    expect(got.some((c) => c.startsWith("CRSISHORT"))).toBe(false);
  });

  it("`All` and `Unprotected` still count ROWS — a flat lane adds no row to the table", () => {
    const got = chips(renderRaw([HELD]));
    expect(got[0]).toBe("All 1");
    expect(got[got.length - 1]).toBe("Unprotected 1");
  });

  it("renders the chip row for a book with NO cycles at all — the state that most needs noticing", () => {
    const got = chips(renderRaw([]));
    expect(got).toEqual(expected([], ON_THIS_NODE));
    expect(got).toContain("All 0");
    expect(got).toContain("Unprotected 0");
  });

  it("renders the row for exactly ONE registered lane and no cycles — the old `> 1` guard's boundary", () => {
    registry.current = { rows: [PAPER_REGISTRY[4]], failed: false, frame: "ok" }; // QC345-003 alone
    try {
      expect(chips(renderRaw([]))).toEqual(["All 0", "QC345 0", "Unprotected 0"]);
    } finally {
      registry.current = LIVE;
    }
  });

  it("a lane in the cycles but NOT in the registry keeps its chip (EXTERNAL, or a deregistered lane's cycle)", () => {
    const got = chips(renderRaw([HELD, cycle("EXTERNAL", "FIG.XNYS")]));
    expect(got).toEqual(expected([HELD, cycle("EXTERNAL", "FIG.XNYS")], ON_THIS_NODE));
    expect(got).toContain("EXTERNAL 1");
  });

  it("a BridgeStrategy cycle and the MANUAL-001 row make ONE `MANUAL` chip, counting the cycle", () => {
    const got = chips(renderRaw([cycle("BridgeStrategy-000", "CRAK.ARCX")]));
    expect(got.filter((c) => c.startsWith("MANUAL"))).toEqual(["MANUAL 1"]);
  });
});

describe("the frame decides whether `unknown` means 'not here' — the rows alone cannot", () => {
  const ALL_UNKNOWN: StrategyRow[] = PAPER_REGISTRY.map((r) => ({ ...r, arm: { state: "unknown" } }));

  it("FIXTURE: the all-unknown rows are the SAME rows under both frames", () => {
    expect(ALL_UNKNOWN.every((r) => r.arm?.state === "unknown")).toBe(true);
    expect(ALL_UNKNOWN.length).toBe(7);
  });

  it("frame unreadable + every row unknown → the cycles' lanes, exactly like no registry (a deploy tick)", () => {
    registry.current = { rows: ALL_UNKNOWN, failed: false, frame: "unreadable" };
    try {
      expect(chips(renderRaw([HELD]))).toEqual(["All 1", "MOMENTUM 1", "Unprotected 1"]);
    } finally {
      registry.current = LIVE;
    }
  });

  it("frame ok + every row unknown → only the manual lane is on this node", () => {
    registry.current = { rows: ALL_UNKNOWN, failed: false, frame: "ok" };
    try {
      expect(chips(renderRaw([HELD]))).toEqual(["All 1", "MANUAL 0", "MOMENTUM 1", "Unprotected 1"]);
    } finally {
      registry.current = LIVE;
    }
  });

  it("an older api that does not say (frame undefined) → the cycles' lanes, never a guess", () => {
    registry.current = { rows: PAPER_REGISTRY, failed: false, frame: undefined };
    try {
      expect(chips(renderRaw([HELD]))).toEqual(["All 1", "MOMENTUM 1", "Unprotected 1"]);
    } finally {
      registry.current = LIVE;
    }
  });
});

describe("today's behaviour, pinned: without a registry the chips are the cycles' lanes", () => {
  it("rows undefined (loading) → cycle lanes only, no `undefined 0`, no crash", () => {
    registry.current = { rows: undefined, failed: false, frame: undefined };
    try {
      expect(chips(renderRaw([HELD]))).toEqual(["All 1", "MOMENTUM 1", "Unprotected 1"]);
    } finally {
      registry.current = LIVE;
    }
  });

  it("read failed → the same fallback; a failed registry must not erase the lanes the cycles prove", () => {
    registry.current = { rows: undefined, failed: true, frame: undefined };
    try {
      expect(chips(renderRaw([HELD]))).toEqual(["All 1", "MOMENTUM 1", "Unprotected 1"]);
    } finally {
      registry.current = LIVE;
    }
  });
});
