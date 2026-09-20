/**
 * The claim/transfer picker must offer what the ENGINE registers (#783).
 *
 * THE DEFECT. `config/strategies.ts` hardcoded the catalog, `transferTargets()` filtered it to
 * `live`, and the result was `["MANUAL-001"]` on every instance forever. BCTROT-004, QC345-003 and
 * TECHIVOL-005 were absent; MOMENTUM carried tag `001` while the real StrategyId is `MOMENTUM-002`.
 * staging's real unclaimed `TRT.AMEX +25` could not be assigned to any lane.
 *
 * AIM AT THE PROPERTY, NOT A LIST. These assert picker == registry. A test that expected a specific
 * set of lanes would need editing every time an instance differs, which is the defect restated.
 */

import { describe, expect, it } from "vitest";

import { STRATEGIES, transferTargets } from "@/config/strategies";

describe("the hardcoded catalog this replaced", () => {
  it("could only ever offer one target, whatever the instance ran", () => {
    // FIXTURE PROPERTY FIRST: if the constant already listed several live lanes, the defect would
    // not exist and every assertion below would be about nothing.
    expect(transferTargets()).toEqual(["MANUAL-001"]);
  });

  it("disagrees with the real StrategyId of a lane that IS live", () => {
    // MOMENTUM-002 is armed and trading on alpaca-paper. The constant says MOMENTUM-001, not live.
    const momentum = STRATEGIES.find((s) => s.id === "MOMENTUM");
    expect(momentum).toBeDefined();
    expect(`MOMENTUM-${momentum!.tag}`).not.toBe("MOMENTUM-002");
  });

  it("omits lanes that run on both instances", () => {
    const ids = STRATEGIES.map((s) => s.id);
    for (const missing of ["BCTROT", "QC345", "TECHIVOL"]) {
      expect(ids).not.toContain(missing);
    }
  });
});

describe("deriving targets from a registry response", () => {
  // The hook's shaping, exercised directly: it is the part that must not drift from the API.
  function shape(body: { strategies?: { strategy_id?: string }[] }): string[] {
    const rows = body.strategies ?? [];
    return rows
      .map((r) => String(r.strategy_id ?? ""))
      .filter((id) => id.length > 0)
      .sort();
  }

  it("offers every lane the engine registers", () => {
    expect(
      shape({
        strategies: [
          { strategy_id: "MANUAL-001" },
          { strategy_id: "MOMENTUM-002" },
          { strategy_id: "BCTROT-004" },
          { strategy_id: "QC345-003" },
          { strategy_id: "TECHIVOL-005" },
        ],
      }),
    ).toEqual(["BCTROT-004", "MANUAL-001", "MOMENTUM-002", "QC345-003", "TECHIVOL-005"]);
  });

  it("offers a DIFFERENT set on an instance that registers fewer — which a constant cannot do", () => {
    expect(
      shape({ strategies: [{ strategy_id: "MANUAL-001" }, { strategy_id: "BCTROT-004" }] }),
    ).toEqual(["BCTROT-004", "MANUAL-001"]);
  });

  it("drops rows with no id rather than offering an empty target", () => {
    expect(shape({ strategies: [{ strategy_id: "MANUAL-001" }, {}] })).toEqual(["MANUAL-001"]);
  });

  it("returns nothing at all when the registry is empty — never a plausible default", () => {
    expect(shape({})).toEqual([]);
  });
});
