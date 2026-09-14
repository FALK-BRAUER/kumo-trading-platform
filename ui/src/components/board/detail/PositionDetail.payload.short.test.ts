/**
 * #855 — the COMMAND PAYLOADS, which is where a signed short stops being a display defect.
 *
 * Every quantity this screen sends to the engine is `position.quantity` verbatim:
 *
 *   `:850` `expected_qty: position.quantity`   FLATTEN
 *   `:760` `qty: position.quantity`            stop_reenter_watch
 *   `:796` `qty: position.quantity`            peak_watch
 *   `:823` `qty: position.quantity`            pyramid_watch
 *   `:645` `quantity: cycle.quantity`          the PositionDTO synthesised when only the projection
 *                                              knows about the position — the source of the other four
 *
 * `backend/api/flatten.py:93` compares `expected_qty != live_qty`, and `live_qty` comes off a Nautilus
 * `Position`, which is non-negative. So a signed `-10` produces:
 *
 *     "the position is 10 now, not the -10 you confirmed — re-read and retry"
 *
 * The operator cannot exit the position. Not a wrong number on a screen — a REFUSED EXIT, phrased as
 * if the book had moved underneath them, on the control they reach for when they want out. The three
 * manager payloads fail the same way at their own `expected_side`/`qty` checks, so arming protection
 * on a short is refused too.
 *
 * THE AST SCAN IN `signedQty.short.test.ts` CANNOT SEE ANY OF THIS. An argument is neither arithmetic
 * nor a comparison, and a property in an object literal is neither. That is the argument for a seam
 * test at the payload rather than a wider scan: the class is "a raw quantity leaves the UI", and the
 * boundary it crosses is a network call, not an operator.
 *
 * HOW THIS IS DRIVEN. The payload closures are built in `PositionDetail`, the container, and handed to
 * `PositionDetailView` as props; `react-dom/server` renders but never invokes them. So the JSX factory
 * is wrapped to record the props of every component element, the container is rendered, and the
 * recorded callbacks are called directly. `react/jsx-dev-runtime` is the factory vitest's transform
 * actually emits — mocking only `react/jsx-runtime` records NOTHING and the test passes vacuously,
 * which is why the first assertion below is that the capture caught the view at all.
 *
 * Everything between the frame and the payload is real: the container, its mutations, and its
 * defaults. Only the data planes and the HTTP client are stubbed, at their module boundaries.
 */
import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { TradeDTO } from "@/lib/api/types";

/** Every component element created during a render, in order. */
const elements: { type: string; props: Record<string, unknown> }[] = [];
const record = (factory: (t: unknown, p: unknown, k: unknown) => unknown) =>
  (type: unknown, props: unknown, key: unknown) => {
    if (typeof type === "function" && type.name) {
      elements.push({ type: type.name, props: props as Record<string, unknown> });
    }
    return factory(type, props, key);
  };

vi.mock("react/jsx-runtime", async (actual) => {
  const a = (await (actual as () => Promise<Record<string, never>>)()) as never as Record<string, never>;
  return { ...a, jsx: record(a.jsx), jsxs: record(a.jsxs) };
});
vi.mock("react/jsx-dev-runtime", async (actual) => {
  const a = (await (actual as () => Promise<Record<string, never>>)()) as never as Record<string, never>;
  return { ...a, jsxDEV: record(a.jsxDEV) };
});

/** Every request the screen issued, as `[endpoint, body]`. */
const sent: [string, Record<string, unknown>][] = [];

vi.mock("@/lib/api/client", async (actual) => {
  const a = (await (actual as () => Promise<Record<string, unknown>>)()) as Record<string, unknown>;
  return {
    ...a,
    flattenPosition: async (p: Record<string, unknown>) => {
      sent.push(["flatten", p]);
      return { command_id: "cmd-1" };
    },
    attachManager: async (p: Record<string, unknown>) => {
      sent.push(["attach", p]);
      return { command_id: "cmd-1" };
    },
    cancelManager: async (p: Record<string, unknown>) => {
      sent.push(["cancel", p as never]);
      return { command_id: "cmd-1" };
    },
    getManagers: async () => [],
  };
});

const MARK = 110;

/** A HELD SHORT cycle, `quantity` spelled either way. Nothing else about it differs. */
const cycle = (quantity: number): TradeDTO =>
  ({
    account_id: "DU1",
    client_id: "IB",
    instrument_id: "AAPL.XNAS",
    strategy_id: "MANUAL-001",
    cycle_id: "c1",
    state: "HELD",
    side: "SHORT",
    quantity,
    is_capital_deployed: true,
    is_engaged: true,
    avg_px_open: 100,
    realized_pnl: "0.00 USD",
    last_px: MARK,
    leg_count: 1,
    opened_ts: 0,
    last_event_ts: 0,
    working_orders: [],
  }) as TradeDTO;

/** The trades frame the container reads. Reassigned per case, before the render. */
let FRAME: TradeDTO[] = [];
/** The POSITIONS frame. Empty by default, so `position` comes from the synthesised row at `:660`;
 *  filled by `pressWithPlaneRow` so `positions.find(...)` wins at `:650` instead. */
let POSITIONS: unknown[] = [];

vi.mock("@/lib/framework/datasource/useSource", () => ({
  useSource: (name: string) => ({
    data:
      name === "trades" ? { trades: FRAME } : name === "positions" ? { positions: POSITIONS } : { orders: [] },
    status: "live",
  }),
  useSources: () => ({}),
  mapToTileStatus: () => "live",
}));
vi.mock("@/lib/framework/instrument", async (actual) => {
  const a = (await (actual as () => Promise<Record<string, unknown>>)()) as Record<string, unknown>;
  return {
    ...a,
    useInstrument: () => ({
      bars: [],
      price: MARK,
      quote: null,
      vwap: null,
      todayRange: null,
      fundamentals: null,
      fills: [],
      status: "live",
    }),
  };
});
// PEAK's toggle is gated on settings being READY. Unready, the closure still exists but the arm is
// refused before it builds a payload — which would make the peak assertion below vacuous.
vi.mock("@/lib/peakDefaults", async (actual) => {
  const a = (await (actual as () => Promise<Record<string, unknown>>)()) as Record<string, unknown>;
  return { ...a, usePeakParams: () => ({ params: { atr_mult: 3 }, ready: true, error: null }) };
});

const { PositionDetail } = await import("./PositionDetail");

interface ViewProps {
  position: { quantity: number; side: string };
  onFlatten: () => void;
  stopReenter: { onToggle: () => void };
  peak: { onToggle: () => void };
  pyramid: { onToggle: (driver: string) => void };
}

/** What `/positions` emits for this position: UNSIGNED quantity on the wire, `ts_last` present — the
 *  field the synthesised row at `:660` does NOT have, which is how the tests tell them apart. */
const planeRow = (quantity: number) => ({
  instrument_id: "AAPL.XNAS",
  side: "SHORT",
  quantity,
  avg_px_open: 100,
  realized_pnl: "0.00 USD",
  strategy_id: "MANUAL-001",
  ts_last: 1,
});

/** Render the container for one cycle, press every command, and return what went to the engine. */
async function press(
  quantity: number,
  positions: unknown[] = [],
): Promise<{ view: ViewProps; sent: [string, Record<string, unknown>][] }> {
  FRAME = [cycle(quantity)];
  POSITIONS = positions;
  elements.length = 0;
  sent.length = 0;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: qc } as never,
      createElement(PositionDetail as never, {
        focus: { kind: "position", positionKey: "MANUAL-001:AAPL.XNAS", instrumentId: "AAPL.XNAS" },
      } as never),
    ),
  );
  const captured = elements.find((e) => e.type === "PositionDetailView");
  // If the factory wrapper missed, `captured` is undefined and every assertion below would throw
  // rather than fail — so this is checked here, once, with a message that says what went wrong.
  if (!captured) throw new Error("the JSX factory capture recorded no PositionDetailView");
  const view = captured.props as unknown as ViewProps;
  view.onFlatten();
  view.stopReenter.onToggle();
  view.peak.onToggle();
  view.pyramid.onToggle("NVDA.XNAS");
  // The mutations resolve on the microtask queue; the payload is recorded inside the mutationFn.
  await new Promise((r) => setTimeout(r, 30));
  return { view, sent: [...sent] };
}

const body = (rows: [string, Record<string, unknown>][], kind: string) =>
  (kind === "flatten"
    ? rows.find(([e]) => e === "flatten")
    : rows.find(([e, p]) => e === "attach" && p.kind === kind))?.[1];

const qtyOf = (rows: [string, Record<string, unknown>][], kind: string) =>
  kind === "flatten"
    ? (body(rows, "flatten") as { expected_qty?: number } | undefined)?.expected_qty
    : (body(rows, kind) as { params?: { qty?: number } } | undefined)?.params?.qty;

const COMMANDS = ["flatten", "stop_reenter_watch", "peak_watch", "pyramid_watch"];

describe("the harness can express the bug", () => {
  it("the capture records the view, and pressing the controls reaches the client", () => {
    // Three ways this test could pass while testing nothing: the factory mock could miss (see the
    // note above about `jsx-dev-runtime`), the container could fall through to its `!position` guard
    // and render nothing, or a toggle could refuse before building a payload.
    return press(10).then(({ view, sent: rows }) => {
      expect(view.position).toBeTruthy();
      expect(rows.map(([e]) => e)).toEqual(["flatten", "attach", "attach", "attach"]);
      for (const kind of COMMANDS) expect(body(rows, kind), `${kind} sent no payload`).toBeTruthy();
    });
  });

  it("the position is synthesised from the CYCLE — the path the payloads read", () => {
    // `positions` is deliberately empty in this harness, so `position` comes from `:645`. If the
    // positions plane answered instead, the synthesised path would never run and the `:645`
    // assertion below would be about code the test never executed.
    return press(-10).then(({ view }) => {
      expect(view.position.side).toBe("SHORT");
      expect(view.position).not.toHaveProperty("ts_last"); // a real PositionDTO carries it; this one does not
    });
  });
});

describe("a command payload carries the SIZE, never the sign (#855)", () => {
  for (const kind of COMMANDS) {
    it(`${kind} sends 10 for a short of 10, spelled unsigned`, async () => {
      // The sibling that passes today, so the failure below points at the spelling.
      const { sent: rows } = await press(10);
      expect(qtyOf(rows, kind)).toBe(10);
    });

    it(`${kind} sends 10 for the same short spelled signed`, async () => {
      // `flatten.py:93` rejects `expected_qty != live_qty` and `live_qty` is non-negative, so -10
      // does not exit the position — it returns "the position is 10 now, not the -10 you confirmed".
      const { sent: rows } = await press(-10);
      expect(qtyOf(rows, kind)).toBe(10);
    });
  }

  it("the side still travels with the command — the sign is dropped, not the direction", () => {
    // A fix that takes `Math.abs()` and stops there would be correct only because `expected_side` is
    // already on every one of these payloads. Pinned so it cannot be removed as redundant.
    return press(-10).then(({ sent: rows }) => {
      expect((body(rows, "flatten") as { expected_side?: string }).expected_side).toBe("SHORT");
      for (const kind of COMMANDS.filter((k) => k !== "flatten")) {
        expect((body(rows, kind) as { params: { expected_side?: string } }).params.expected_side).toBe("SHORT");
      }
    });
  });
});

describe("the synthesised PositionDTO is where all four payloads get it (#855)", () => {
  it("a signed cycle does not become a signed position", () => {
    // `:645` copies `cycle.quantity` into the PositionDTO the whole screen then reads — the four
    // payloads, the market value, the flatten copy. Fixing it here fixes them together; fixing the
    // four call sites and leaving this one leaves the next reader to find out.
    return press(-10).then(({ view }) => expect(view.position.quantity).toBe(10));
  });

  it("an unsigned cycle is unchanged", () => {
    return press(10).then(({ view }) => expect(view.position.quantity).toBe(10));
  });
});


/**
 * THE PLANE ROW PATH — the one the four `magnitude()` calls actually guard (#855).
 *
 * Every test above leaves the `positions` plane EMPTY, so `position` comes from the synthesised DTO at
 * `PositionDetail.tsx:660`, whose quantity is already `magnitude(cycle.quantity)`. That makes the four
 * payload builders read an value that has been repaired upstream — so deleting `magnitude(` from
 * `:775`, `:811`, `:838` and `:865` left all twelve payload tests green. Two layers covering each
 * other, and neither independently pinned.
 *
 * Here the plane answers, `positions.find(...)` wins at `:650`, and the raw wire value reaches the
 * payload builders. This is the branch that runs on a live stack: the positions plane is populated
 * whenever the engine knows about the position, which is the ordinary case.
 */
describe("a payload built from the POSITIONS plane carries the magnitude (#855)", () => {
  it("the plane row wins over the synthesised one", () => {
    // Vacuity guard, and the whole point of the block: if `positions.find` did not win, these tests
    // would be re-testing `:660` under a different name. `ts_last` is the discriminator — a real
    // PositionDTO carries it and the synthesised row does not.
    return press(10, [planeRow(-10)]).then(({ view }) => {
      expect(view.position).toHaveProperty("ts_last");
      expect(view.position.side).toBe("SHORT");
    });
  });

  for (const kind of COMMANDS) {
    it(`${kind} sends 10 when the plane row is spelled signed`, async () => {
      const { sent: rows } = await press(10, [planeRow(-10)]);
      expect(qtyOf(rows, kind)).toBe(10);
    });
  }

  it("an unsigned plane row is unchanged", async () => {
    const { sent: rows } = await press(10, [planeRow(10)]);
    for (const kind of COMMANDS) expect(qtyOf(rows, kind), kind).toBe(10);
  });
});
