import { describe, it, expect } from "vitest";
import type { FC } from "react";
import { registerDetail, resolveDetail, type DetailProps } from "./registry";

const Dummy: FC<DetailProps> = () => null;

describe("DetailRegistry", () => {
  it("resolves an exact (kind, strategy) match, else the (kind, '*') fallback, else undefined", () => {
    registerDetail({ id: "t.order.default", kind: "order", strategy: "*", component: Dummy });
    registerDetail({ id: "t.order.momentum", kind: "order", strategy: "MOMENTUM", component: Dummy });

    expect(resolveDetail("order", "MOMENTUM")?.id).toBe("t.order.momentum"); // exact
    expect(resolveDetail("order", "ETF_AUTO")?.id).toBe("t.order.default"); // fallback to *
    expect(resolveDetail("order", "*")?.id).toBe("t.order.default"); // the default itself
    expect(resolveDetail("position", "MANUAL")).toBeUndefined(); // nothing registered for the kind
  });

  it("registration is idempotent by (kind, strategy) — a re-register is a no-op (HMR safe)", () => {
    registerDetail({ id: "t.symbol.a", kind: "symbol", strategy: "*", component: Dummy });
    registerDetail({ id: "t.symbol.b", kind: "symbol", strategy: "*", component: Dummy });
    expect(resolveDetail("symbol", "*")?.id).toBe("t.symbol.a"); // first wins, no throw
  });
});
