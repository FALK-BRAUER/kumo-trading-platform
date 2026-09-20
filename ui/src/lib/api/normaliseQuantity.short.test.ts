/**
 * #855 — the frame decode is where a signed quantity stops being reachable, so it needs a test.
 *
 * WRITTEN BECAUSE THE MUTATION SURVIVED. Reverting `r.quantity = Math.abs(r.quantity)` to
 * `r.quantity = r.quantity` left all 85 `*.short.test.ts` tests green: every one of them stubs the data
 * planes or renders a component directly, so not one drove a decode. An unbitten mechanism is an
 * unearned one, and this repo has shipped that shape repeatedly — a change that anchors on something
 * absent does nothing, quietly.
 *
 * Three of the four tests below drive a REAL decode path rather than the helper, because a green test
 * on the helper says nothing about whether anything calls it:
 *
 *   `getPositions()`            the REST client, through a stubbed `fetch`
 *   `wsManager.onMessage()`     the WebSocket snapshot path, through the real singleton
 *   `fetchRest` in `useSource`  NOT drivable — it is a module-private function reached only through a
 *                               React hook and react-query, so it is covered by the coverage assertion
 *                               at the bottom and that limitation is stated there rather than hidden.
 *
 * `wsManager` is reachable in the node environment precisely because `connect()` returns early when
 * `WebSocket` is undefined: `subscribe()` still registers the topic entry, so a frame can be handed to
 * the decoder without a socket existing.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";

import { normaliseQuantity, normalisedNegatives, resetNormalisedNegatives } from "./normaliseQuantity";
import { getPositions, getTransfers } from "./client";
import { wsManager } from "@/lib/framework/datasource/ws-manager";
import { canonicalTopicKey, type Topic } from "@/lib/framework/datasource/protocol";

const shortRow = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  instrument_id: "AAPL.XNAS",
  side: "SHORT",
  quantity: -10,
  avg_px_open: 100,
  realized_pnl: "0.00 USD",
  strategy_id: "MOMENTUM-002",
  ...over,
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetNormalisedNegatives();
});

describe("the fixture can express the bug", () => {
  it("the row really does arrive signed", () => {
    // Vacuity guard: an unsigned fixture is already normalised and every assertion below would pass
    // against a decoder that did nothing at all.
    expect(shortRow().quantity).toBe(-10);
    expect(Math.abs(shortRow().quantity as number)).toBe(10);
    expect(shortRow().side).toBe("SHORT");
  });
});

describe("normaliseQuantity", () => {
  it("absolutises quantity on trades, positions and external rows", () => {
    const frame = normaliseQuantity({
      trades: [shortRow()],
      positions: [shortRow()],
      external: [shortRow()],
    });
    expect(frame.trades[0].quantity).toBe(10);
    expect(frame.positions[0].quantity).toBe(10);
    expect(frame.external[0].quantity).toBe(10);
    // The direction is still on the row; only its duplicate on `quantity` is gone.
    expect(frame.trades[0].side).toBe("SHORT");
  });

  it("leaves venue_qty alone — its sign and its zero both mean something", () => {
    // `venue_qty` is the broker's three-state answer: a number, 0 meaning "answered, holds none" (a
    // phantom, #807), null meaning "not asked". Absolutising it would make a short holding read
    // identical to a long one at the exact surface built to catch positions the engine has lost.
    const frame = normaliseQuantity({ external: [shortRow({ venue_qty: -10 })] });
    expect(frame.external[0].venue_qty).toBe(-10);
    expect(frame.external[0].quantity).toBe(10); // the sibling field WAS normalised, so the scan ran
    const phantom = normaliseQuantity({ external: [shortRow({ venue_qty: 0 })] });
    expect(phantom.external[0].venue_qty).toBe(0);
  });

  it("passes anything that is not a frame of rows straight through", () => {
    // It runs on EVERY decode, including bars, fills and error payloads. A decoder that threw on an
    // unexpected shape would take the whole plane down for a shape it was never meant to touch.
    expect(normaliseQuantity(null)).toBeNull();
    expect(normaliseQuantity(undefined)).toBeUndefined();
    expect(normaliseQuantity({ bars: [{ close: 1 }] })).toEqual({ bars: [{ close: 1 }] });
    expect(normaliseQuantity({ trades: "not an array" })).toEqual({ trades: "not an array" });
    expect(normaliseQuantity({ trades: [null, 7, { quantity: -3 }] })).toEqual({
      trades: [null, 7, { quantity: 3 }],
    });
  });
});

describe("the REST decode normalises (seam)", () => {
  it("GET /positions cannot hand the UI a signed quantity", async () => {
    vi.stubGlobal("fetch", async () => ({
      ok: true,
      json: async () => ({ positions: [shortRow()] }),
    }));
    const res = await getPositions();
    expect(res.positions[0].quantity).toBe(10);
    expect(res.positions[0].side).toBe("SHORT");
  });
});

describe("the WebSocket decode normalises (seam)", () => {
  it("a snapshot frame cannot hand the UI a signed quantity", () => {
    const topic: Topic = { channel: "trades", params: {} };
    const release = wsManager.subscribe(topic);
    try {
      // `onMessage` is the real decoder; `private` is erased at runtime. Driving it is the point —
      // asserting on `normaliseQuantity` alone would not notice this call site being deleted.
      (wsManager as unknown as { onMessage(raw: string): void }).onMessage(
        JSON.stringify({
          event: "data",
          topic,
          payload: { frame_type: "snapshot", data: { trades: [shortRow()] } },
        }),
      );
      const data = wsManager.getTopicState(canonicalTopicKey(topic))?.data as
        | { trades: { quantity: number; side: string }[] }
        | undefined;
      expect(data, "the frame was not stored — the decode never ran").toBeTruthy();
      expect(data!.trades[0].quantity).toBe(10);
      expect(data!.trades[0].side).toBe("SHORT");
    } finally {
      release();
    }
  });
});

describe("the transfers plane normalises (seam)", () => {
  it("GET /transfers cannot hand the UI a signed quantity", async () => {
    // `getTransfers` returns rows carrying `side` and `quantity` under the key `transfers`, which was
    // absent from ROW_KEYS: the fix normalised one endpoint of the client's twenty-four and the old
    // coverage test could not tell, because it only asked whether the FILE mentioned the helper.
    vi.stubGlobal("fetch", async () => ({
      ok: true,
      json: async () => ({ transfers: [{ transfer_id: "t1", side: "SHORT", quantity: -10 }] }),
    }));
    const res = await getTransfers();
    expect(res.transfers[0].quantity).toBe(10);
    expect(res.transfers[0].side).toBe("SHORT");
  });
});

describe("a repaired quantity is a contract violation, and it says so", () => {
  it("counts every negative it absolutises and warns once", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(normalisedNegatives()).toBe(0); // the fixture starts from a known zero
      normaliseQuantity({ trades: [shortRow(), shortRow()], positions: [shortRow()] });
      // THE COUNT MOVES. A silent repair on a plane whose contract says the value cannot be negative
      // is the shape that hides the first producer to break it — "no alarms" reading as "nothing
      // wrong". Three rows repaired, three counted.
      expect(normalisedNegatives()).toBe(3);
      // ONCE, not per row: a book of forty shorts would bury the console it exists to draw attention to.
      expect(warn).toHaveBeenCalledTimes(1);
      expect(String(warn.mock.calls[0][0])).toContain("NEGATIVE quantity");
    } finally {
      warn.mockRestore();
    }
  });

  it("counts nothing when the contract holds", () => {
    // The vacuity guard for the counter: an unsigned book must not tick it, or a non-zero count says
    // nothing about whether a violation happened.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      normaliseQuantity({ trades: [shortRow({ quantity: 10 })], positions: [shortRow({ quantity: 0 })] });
      expect(normalisedNegatives()).toBe(0);
      expect(warn).not.toHaveBeenCalled();
    } finally {
      warn.mockRestore();
    }
  });
});

/**
 * COVERAGE OVER THE RESPONSE TYPES, NOT OVER THE FILES.
 *
 * The old version asked whether each of three files mentioned `normaliseQuantity(`. A decoder
 * normalising one endpoint of twenty-four passed it, and `getTransfers` was exactly that — the client
 * mentioned the helper, and transfer rows went through unnormalised.
 *
 * This reads the GENERATED SCHEMA instead and requires every DTO carrying a `quantity` to be
 * classified: either position-shaped, in which case its frame key must be in `ROW_KEYS`, or exempt
 * with a stated reason. A new quantity-bearing type appearing in a regenerated schema fails here
 * rather than arriving unnoticed.
 */
describe("every quantity-bearing response type is classified", () => {
  /** DTO -> the frame key its rows arrive under. Each of these must be in `ROW_KEYS`. */
  const POSITION_SHAPED: Record<string, string> = {
    TradeDTO: "trades",
    PositionDTO: "positions",
    ExternalActivityDTO: "external",
    TransferDTO: "transfers",
  };

  /** DTO -> why it is NOT normalised. Every entry is a claim a reader can check. */
  const EXEMPT: Record<string, string> = {
    FillDTO: "a fill quantity is a Nautilus Quantity (cannot be negative); its direction is BUY/SELL",
    OrderDTO: "an order quantity is a Nautilus Quantity; its direction is BUY/SELL, not LONG/SHORT",
    WorkingOrderDTO: "same as OrderDTO — a resting order's size, not a position's",
    OrderRequest: "a request body the UI SENDS; nothing decodes it",
    BracketRequest: "a request body the UI SENDS; nothing decodes it",
    ModifyOrderRequest: "a request body the UI SENDS; nothing decodes it",
    TransferRequestBody: "a request body the UI SENDS; nothing decodes it",
  };

  /** Every schema type declaring a `quantity` field, read out of the generated file. */
  function quantityBearingTypes(): string[] {
    const src = readFileSync(new URL("./schema.ts", import.meta.url), "utf8");
    const out: string[] = [];
    let current: string | null = null;
    for (const line of src.split("\n")) {
      const decl = /^\s{8}(\w+): \{/.exec(line);
      if (decl) current = decl[1];
      if (/^\s+quantity\??:/.test(line) && current) out.push(current);
    }
    return [...new Set(out)];
  }

  const ROW_KEYS_IN_SOURCE = (() => {
    const src = readFileSync(new URL("./normaliseQuantity.ts", import.meta.url), "utf8");
    const m = /const ROW_KEYS = \[([^\]]*)\]/.exec(src);
    return (m?.[1] ?? "").split(",").map((k) => k.trim().replace(/^["']|["']$/g, "")).filter(Boolean);
  })();

  it("the schema really does declare quantity on several types", () => {
    // Vacuity guard. A parser that matched nothing would classify an empty set and pass forever.
    const types = quantityBearingTypes();
    expect(types.length).toBeGreaterThan(5);
    expect(types).toContain("TradeDTO");
    expect(types).toContain("FillDTO");
    expect(ROW_KEYS_IN_SOURCE.length).toBeGreaterThan(0);
  });

  it("each one is either normalised or exempted with a reason", () => {
    const unclassified = quantityBearingTypes().filter((t) => !(t in POSITION_SHAPED) && !(t in EXEMPT));
    expect(unclassified, "a new quantity-bearing type appeared — normalise it or exempt it").toEqual([]);
  });

  it("every position-shaped type's frame key is actually in ROW_KEYS", () => {
    // The half that catches the `transfers` defect: a type can be classified as position-shaped in
    // this test and still be missing from the module that does the work.
    const missing = Object.entries(POSITION_SHAPED)
      .filter(([, key]) => !ROW_KEYS_IN_SOURCE.includes(key))
      .map(([dto, key]) => `${dto} -> ${key}`);
    expect(missing).toEqual([]);
  });

  it("the three decode points still call the normaliser", () => {
    // Kept, narrowed to what it can honestly claim: `fetchRest` is module-private behind a React hook
    // and react-query, so it has no seam a node test can drive. The other two are driven above; this
    // exists to catch that one call being deleted.
    for (const path of [
      "src/lib/api/client.ts",
      "src/lib/framework/datasource/useSource.ts",
      "src/lib/framework/datasource/ws-manager.ts",
    ]) {
      const src = readFileSync(new URL(`../../../${path}`, import.meta.url), "utf8");
      expect(src, `${path} never calls the normaliser`).toMatch(/normaliseQuantity\(/);
    }
  });
});
