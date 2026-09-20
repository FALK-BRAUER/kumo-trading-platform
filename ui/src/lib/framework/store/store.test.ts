/** Store navigation invariants. */
import { describe, expect, it, beforeEach } from "vitest";

import { useCockpitStore } from "./index";

const reset = () =>
  useCockpitStore.setState({ layouts: [], activeViewId: null, focus: null, detailOpen: false });

describe("detail surface vs view switching", () => {
  beforeEach(reset);

  it("closes the detail when the view changes", () => {
    // The detail renders OVER the tile grid, so leaving it open made a tab press look like it did nothing.
    const s = useCockpitStore.getState();
    s.openDetail({ kind: "unclaimed", instrumentId: "FIG.XNYS", sourceStrategyId: "EXTERNAL", side: "LONG" });
    expect(useCockpitStore.getState().detailOpen).toBe(true);

    useCockpitStore.getState().setActiveView("watch");

    expect(useCockpitStore.getState().detailOpen).toBe(false);
    expect(useCockpitStore.getState().activeViewId).toBe("watch");
  });

  it("keeps the focus so the search chip and order ticket survive navigation", () => {
    const s = useCockpitStore.getState();
    s.openDetail({ kind: "unclaimed", instrumentId: "FIG.XNYS", sourceStrategyId: "EXTERNAL", side: "LONG" });

    useCockpitStore.getState().setActiveView("orders");

    expect(useCockpitStore.getState().focus).not.toBeNull();
  });
});
