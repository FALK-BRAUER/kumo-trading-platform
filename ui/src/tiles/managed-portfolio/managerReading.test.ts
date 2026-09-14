/**
 * Naming the manager, and telling a live failure from a fossil (#402 second half, #400).
 *
 * `armedReading` names a resting ENTRY from the cycle's own orders. It cannot name the MECHANISM: the
 * trades plane carries `manager_id: null` even when a manager row exists (verified on CRAK,
 * 2026-08-21), so the kind must come from the `/managers` plane.
 *
 * THE STALENESS RULE IS STRUCTURAL, NOT A TIMEOUT — and it is the whole point. On 2026-08-21 a
 * `peak_watch` that FAILED on 2026-08-11, against a cycle that closed nine days earlier, was read as a
 * live outage and reported as "PEAK is dead". It was not: the bug it named had been fixed 38 minutes
 * after that row was written. Meanwhile A.XNYS's rearm failed the SAME afternoon on an off-tick price
 * (#401) and went unnoticed in the same undated list.
 *
 * `client.ts` already states the rule on the type: "a stale manager from a closed cycle must not mask a
 * newer one on the position's current cycle — match on this before falling back". An age threshold
 * would be a guess about how long is too long; the cycle boundary is a fact.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ageLabel, failureLabel, managersFor, mechanismLabel } from "./managerReading";
import type { Manager } from "@/lib/api/client";

const CUR = "ALPACA:A.XNYS:MANUAL-001:1787241696655777757";
const OLD = "ALPACA:WDAY.XNAS:MANUAL-001:1786471337431438299";

const m = (over: Partial<Manager>): Manager =>
  ({
    manager_id: "id-1",
    kind: "stop_reenter_watch",
    instrument_id: "A.XNYS",
    strategy_id: "MANUAL-001",
    cycle_id: CUR,
    leash: "AUTO",
    state: "ARMED",
    params: {},
    error: null,
    ...over,
  }) as Manager;

/** 2026-08-20 18:30Z — ten minutes after A's rearm failed. */
const NOW = Date.parse("2026-08-20T18:30:00Z");

describe("managers are scoped to the CURRENT cycle (#402/#400)", () => {
  it("the fixture contains a manager on a DIFFERENT cycle", () => {
    // The fixture's own property first. With every row on the same cycle, the scoping under test does
    // nothing and every assertion below would pass with it deleted.
    expect(OLD).not.toBe(CUR);
  });

  it("a FAILED manager on the current cycle is reported", () => {
    // A.XNYS, live 2026-08-20 18:20:41Z — the failure that mattered and was invisible.
    const failed = m({ state: "FAILED", kind: "stop_reenter_rearm", error: "price 131.44614999999993 is not a valid tick for A.XNYS" });
    expect(managersFor([failed], "A.XNYS", "MANUAL-001", CUR).failed).toEqual([failed]);
  });

  it("a FAILED manager from a CLOSED cycle is NOT reported", () => {
    // WDAY's nine-day-old peak_watch. This is the row that produced a false "PEAK is dead" report.
    const fossil = m({ instrument_id: "WDAY.XNAS", cycle_id: OLD, state: "FAILED", kind: "peak_watch" });
    const out = managersFor([fossil], "WDAY.XNAS", "MANUAL-001", CUR);
    expect(out.failed).toEqual([]);
    expect(out.live).toEqual([]);
  });

  it("NOTHING on this cycle is a real answer — it does not fall back to an older one", () => {
    // The dangerous convenience. If "no rows on the current cycle" silently widened to "any row for
    // this symbol", every closed cycle's fossils would reappear and the scoping would be decorative.
    const fossil = m({ cycle_id: OLD, state: "FAILED" });
    expect(managersFor([fossil], "A.XNYS", "MANUAL-001", CUR).failed).toEqual([]);
  });

  it("falls back to instrument+strategy ONLY when the position has no cycle id", () => {
    // Pre-#68 rows, and flat cycles with no id. Documented degradation, not a default.
    const any = m({ cycle_id: OLD, state: "FAILED" });
    expect(managersFor([any], "A.XNYS", "MANUAL-001", null).failed).toEqual([any]);
  });

  it("another symbol's manager never leaks onto this row", () => {
    const other = m({ instrument_id: "APA.XNAS" });
    expect(managersFor([other], "A.XNYS", "MANUAL-001", CUR).live).toEqual([]);
  });

  it("another STRATEGY's manager never leaks either", () => {
    // AEM is held by MANUAL and MOMENTUM at once; managers are armed per position, so one row must not
    // claim the other's.
    const other = m({ strategy_id: "MOMENTUM-002" });
    expect(managersFor([other], "A.XNYS", "MANUAL-001", CUR).live).toEqual([]);
  });

  it("terminal-but-not-failed states are neither live nor failed", () => {
    // APPLIED and CANCELLED are over. Showing them would put a permanent noun on every row that ever
    // had a manager — WDAY carries five APPLIED peak_watch rows.
    for (const state of ["APPLIED", "CANCELLED"] as const) {
      const done = m({ state });
      const out = managersFor([done], "A.XNYS", "MANUAL-001", CUR);
      expect(out.live).toEqual([]);
      expect(out.failed).toEqual([]);
    }
  });
});

describe("the mechanism gets a name (#402)", () => {
  it("names the kinds an operator actually arms", () => {
    expect(mechanismLabel("peak_watch")).toBe("peak");
    expect(mechanismLabel("stop_reenter_watch")).toBe("stop & reenter");
    expect(mechanismLabel("stop_reenter_rearm")).toBe("stop & reenter");
    expect(mechanismLabel("pyramid_watch")).toBe("pyramid");
  });

  it("an UNKNOWN kind falls back to the raw kind, not to a category", () => {
    // A name we do not recognise is still more informative than "manager", and a new kind must not
    // silently render as something generic.
    expect(mechanismLabel("brand_new_kind")).toBe("brand_new_kind");
  });
});

describe("age is context, never the relevance test (#400)", () => {
  it("reads minutes, hours and days", () => {
    expect(ageLabel("2026-08-20T18:20:00Z", NOW)).toBe("10m");
    expect(ageLabel("2026-08-20T14:30:00Z", NOW)).toBe("4h");
    expect(ageLabel("2026-08-11T18:03:26Z", NOW)).toBe("9d");
  });

  it("no timestamp is null, not zero", () => {
    // The API only began returning these in #400; an older engine sends neither. "0m ago" would be a
    // confident lie about a row of unknown age — exactly what this pair of issues is about.
    expect(ageLabel(null, NOW)).toBeNull();
    expect(ageLabel(undefined, NOW)).toBeNull();
    expect(ageLabel("not-a-date", NOW)).toBeNull();
  });

  it("a clock skew does not produce a negative age", () => {
    expect(ageLabel("2026-08-21T00:00:00Z", NOW)).toBeNull();
  });

  it("the failure line carries the age when known and omits it when not", () => {
    const failed = m({ state: "FAILED", kind: "stop_reenter_rearm", updated_at: "2026-08-20T18:20:41Z" });
    expect(failureLabel(failed, NOW)).toBe("stop & reenter FAILED 9m ago");
    expect(failureLabel(m({ state: "FAILED", kind: "peak_watch" }), NOW)).toBe("peak FAILED");
  });

  it("prefers updated_at — for a terminal row that is when it DIED", () => {
    // A FAILED row is not touched again, so `updated_at` is the moment of death; `created_at` is when
    // the manager was armed, which can be days earlier and is a different question.
    const failed = m({
      state: "FAILED",
      kind: "peak_watch",
      created_at: "2026-08-11T18:03:26Z",
      updated_at: "2026-08-20T18:20:41Z",
    });
    expect(failureLabel(failed, NOW)).toBe("peak FAILED 9m ago");
  });
});


describe("the tile binds the plane and renders both (#402)", () => {
  // COMMENTS STRIPPED. Three assertions elsewhere tonight passed with the code deleted because they
  // matched a docstring; do not repeat it.
  const strip = (src: string) => src.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
  const TILE = strip(readFileSync(join(import.meta.dirname, "ManagedPortfolioTile.tsx"), "utf8"));
  const DEF = strip(readFileSync(join(import.meta.dirname, "definition.ts"), "utf8"));
  const SOURCES = strip(readFileSync(join(import.meta.dirname, "..", "..", "config", "datasources.ts"), "utf8"));

  it("the scans read the real files", () => {
    expect(TILE).toContain("ManagedPortfolioTile");
    expect(DEF).toContain("managedPortfolioDefinition");
    expect(SOURCES).toContain("registerSource");
  });

  it("the managers source is REGISTERED", () => {
    // There is no WS channel for managers — the engine publishes budget/command_ack/equity_curve/
    // order/positions/session and nothing else. REST is the only transport available.
    expect(SOURCES).toMatch(/name:\s*"managers"/);
    expect(SOURCES).toMatch(/endpoint:\s*"\/managers"/);
  });

  it("the tile BINDS it — declaring a source nobody binds delivers nothing", () => {
    expect(DEF).toMatch(/dataSources:.*"managers"/);
  });

  it("the tile reads the plane rather than fetching", () => {
    // Framework rule: tiles never fetch directly; the container owns all data wiring.
    expect(TILE).toMatch(/data\.managers/);
    expect(TILE).not.toMatch(/getManagers\(/);
  });

  it("the mechanism reaches the armed line", () => {
    expect(TILE).toMatch(/mechanisms\.join/);
  });

  it("a FAILED manager is rendered, scoped and aged", () => {
    expect(TILE).toMatch(/failures\.map/);
    expect(TILE).toMatch(/failureLabel\(f, nowMs\)/);
    expect(TILE).toMatch(/managersFor\(managers, c\.instrument_id, c\.strategy_id, c\.cycle_id\)/);
  });
});
