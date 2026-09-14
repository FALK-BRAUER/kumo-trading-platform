import { describe, it, expect } from "vitest";
import { groupByPosition, positionKeyFor, type Group } from "./grouping";
import type { TradeDTO } from "@/lib/api/types";

/** The real case: AEM held by MANUAL-001 and MOMENTUM-002 at once (#273). */
function trade(over: Partial<TradeDTO> = {}): TradeDTO {
  return {
    instrument_id: "AEM.XNYS",
    strategy_id: "MANUAL-001",
    cycle_id: "c1",
    side: "LONG",
    quantity: 2,
    avg_px_open: 180,
    realized_pnl: "0.00 USD",
    is_capital_deployed: true,
    state: "HELD",
    ...over,
  } as TradeDTO;
}

describe("groupByPosition", () => {
  it("gives a symbol held by two strategies TWO rows, not one netted row", () => {
    // Operator, 2026-08-13, after buying AEM manually while MOMENTUM-002 already held it: the tile showed a
    // single row of NET 56 with both strategies listed underneath as a footnote. Strategies are the unit
    // of account here — each is its own sleeve with its own P&L, Nautilus keeps them genuinely separate
    // (NETTING ids are `{instrument}-{strategy_id}`), and managers are armed per position.
    const rows = groupByPosition([
      trade({ strategy_id: "MANUAL-001", quantity: 2 }),
      trade({ strategy_id: "MOMENTUM-002", quantity: 54, cycle_id: "c2" }),
    ]);

    expect(rows).toHaveLength(2);
    expect(rows.map((r) => r.strategyId).sort()).toEqual(["MANUAL-001", "MOMENTUM-002"]);
    expect(rows.map((r) => r.netQty).sort((a, b) => a - b)).toEqual([2, 54]);
    // Emphatically NOT one row of 56.
    expect(rows.some((r) => r.netQty === 56)).toBe(false);
  });

  it("gives every row exactly one strategy, so the detail pane always has a key", () => {
    // The second symptom: tapping the merged row opened the CHART, because `heldPositionKey` needs a
    // single `strategy_id:instrument_id` and a netted row had none to offer. The two multi-strategy
    // positions could not be opened or managed from this tab at all.
    const rows = groupByPosition([
      trade({ strategy_id: "MANUAL-001" }),
      trade({ strategy_id: "MOMENTUM-002", cycle_id: "c2" }),
    ]);
    for (const r of rows as Group[]) {
      expect(r.strategyId).toBeTruthy();
      expect(new Set(r.cycles.map((c) => c.strategy_id)).size).toBe(1);
    }
  });

  it("still merges multiple cycles of the SAME strategy on one instrument", () => {
    // The grouping narrowed by strategy, not to one-row-per-cycle. A strategy that opened, closed and
    // reopened the same name is still one position for display purposes.
    const rows = groupByPosition([
      trade({ cycle_id: "c1", quantity: 10 }),
      trade({ cycle_id: "c2", quantity: 5 }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].netQty).toBe(15);
    expect(rows[0].cycles).toHaveLength(2);
  });

  it("keeps different instruments apart even under the same strategy", () => {
    const rows = groupByPosition([trade(), trade({ instrument_id: "FIG.XNYS", cycle_id: "c2" })]);
    expect(rows).toHaveLength(2);
    expect(rows.map((r) => r.instrumentId).sort()).toEqual(["AEM.XNYS", "FIG.XNYS"]);
  });

  it("weights the average across cycles of one strategy rather than taking the last", () => {
    // Pre-existing behaviour that must survive the regrouping: 100 @ 100 and 50 @ 130 is $110, not $130.
    const rows = groupByPosition([
      trade({ cycle_id: "c1", quantity: 100, avg_px_open: 100 }),
      trade({ cycle_id: "c2", quantity: 50, avg_px_open: 130 }),
    ]);
    expect(rows[0].avg).toBeCloseTo(110, 6);
  });

  it("a SHORT leg nets negatively within its own row and cannot cancel another strategy's long", () => {
    // The netting was worst here: opposite-side legs would have produced a NET matching neither.
    const rows = groupByPosition([
      trade({ strategy_id: "MANUAL-001", side: "LONG", quantity: 50 }),
      trade({ strategy_id: "MOMENTUM-002", side: "SHORT", quantity: 50, cycle_id: "c2" }),
    ]);
    expect(rows).toHaveLength(2);
    expect((rows as Group[]).map((r) => r.netQty).sort((a, b) => a - b)).toEqual([-50, 50]);
  });
});

describe("positionKeyFor", () => {
  it("resolves for a held row even with SEVERAL cycles behind it", () => {
    // `heldPositionKey` returns a key only when exactly ONE cycle is held, so a strategy that opened,
    // closed and reopened the same name still fell through to the chart. A row is now one instrument and
    // one strategy, which is all the key needs. (codex review, High.)
    const rows = groupByPosition([
      trade({ cycle_id: "c1", quantity: 10 }),
      trade({ cycle_id: "c2", quantity: 5 }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].cycles).toHaveLength(2);
    expect(positionKeyFor(rows[0])).toBe("MANUAL-001:AEM.XNYS");
  });

  it("is null for a row that holds nothing, so flat/ARMED rows still open the symbol surface", () => {
    const rows = groupByPosition([trade({ is_capital_deployed: false, state: "ARMED" })]);
    expect(positionKeyFor(rows[0])).toBeNull();
  });

  it("gives the two legs of one symbol DIFFERENT keys", () => {
    const rows = groupByPosition([
      trade({ strategy_id: "MANUAL-001" }),
      trade({ strategy_id: "MOMENTUM-002", cycle_id: "c2" }),
    ]);
    const keys = (rows as Group[]).map(positionKeyFor).sort();
    expect(keys).toEqual(["MANUAL-001:AEM.XNYS", "MOMENTUM-002:AEM.XNYS"]);
  });
});

describe("key and order stability", () => {
  it("cannot collide when an id contains the separator character", () => {
    // Both ids are unconstrained strings in the generated schema, so any single-character separator can
    // be forged. JSON encodes the boundary instead. (codex review, Low.)
    const rows = groupByPosition([
      trade({ instrument_id: "A:B", strategy_id: "C" }),
      trade({ instrument_id: "A", strategy_id: "B:C", cycle_id: "c2" }),
    ]);
    expect(rows).toHaveLength(2);
  });

  it("orders two rows of one symbol deterministically", () => {
    const forward = groupByPosition([
      trade({ strategy_id: "MOMENTUM-002" }),
      trade({ strategy_id: "MANUAL-001", cycle_id: "c2" }),
    ]);
    const reverse = groupByPosition([
      trade({ strategy_id: "MANUAL-001", cycle_id: "c2" }),
      trade({ strategy_id: "MOMENTUM-002" }),
    ]);
    expect((forward as Group[]).map((r) => r.strategyId)).toEqual(
      (reverse as Group[]).map((r) => r.strategyId),
    );
  });
});
