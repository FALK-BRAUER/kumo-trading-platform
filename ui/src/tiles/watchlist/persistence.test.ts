import { describe, it, expect } from "vitest";
import { readPersistedConfig, writePersistedConfig } from "./persistence";
import { watchlistConfigSchema } from "./schema";

describe("readPersistedConfig", () => {
  it("returns null when there is no window (SSR/node) — same pattern as lib/theme.ts", () => {
    expect(readPersistedConfig("watch-1")).toBeNull();
  });
});

describe("writePersistedConfig", () => {
  it("no-ops without throwing when there is no window", () => {
    expect(() => writePersistedConfig("watch-1", { symbols: [], sortDir: "asc" })).not.toThrow();
  });
});

// The schema-validation behavior readPersistedConfig relies on (safeParse before trusting a stored
// value) — exercised directly since the node test env has no real localStorage to round-trip through
// (see readPersistedConfig's own "no window" test above; this covers the OTHER half, what happens to a
// value that DID come back from storage).
describe("watchlistConfigSchema (persistence's trust boundary)", () => {
  it("accepts a valid persisted shape", () => {
    const result = watchlistConfigSchema.safeParse({
      symbols: ["AAPL.XNAS"], kpis: ["volume"], sortBy: "pe", sortDir: "desc", statusFilter: ["HOLD"],
    });
    expect(result.success).toBe(true);
  });

  it("rejects a corrupted/stale-schema sortDir — never trusted as-is", () => {
    const result = watchlistConfigSchema.safeParse({ symbols: [], sortDir: "sideways" });
    expect(result.success).toBe(false);
  });

  it("rejects a statusFilter value recommend() can't produce on this tile (codex review, Phase 4)", () => {
    // A corrupted/stale-schema value here would otherwise pass through and filter out every row (nothing
    // reports a status outside WATCH/HOLD/EXIT) instead of degrading to "no override".
    const result = watchlistConfigSchema.safeParse({ symbols: [], statusFilter: ["BOGUS"] });
    expect(result.success).toBe(false);
  });

  it("accepts a valid tierFilter/cloudFilter (Phase 5)", () => {
    const result = watchlistConfigSchema.safeParse({
      symbols: [], sortDir: "asc", tierFilter: ["++", "?"], cloudFilter: ["above", "below"],
    });
    expect(result.success).toBe(true);
  });

  it("rejects a tierFilter value outside the Blue Flag tier set (Phase 5)", () => {
    const result = watchlistConfigSchema.safeParse({ symbols: [], tierFilter: ["+++++"] });
    expect(result.success).toBe(false);
  });

  it("rejects a cloudFilter value outside above/in/below (Phase 5)", () => {
    const result = watchlistConfigSchema.safeParse({ symbols: [], cloudFilter: ["sideways"] });
    expect(result.success).toBe(false);
  });

  it("defaults sortDir to asc when omitted", () => {
    const result = watchlistConfigSchema.safeParse({ symbols: [] });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.sortDir).toBe("asc");
  });
});
