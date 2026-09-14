import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { attribute, dayMove, dayMoveByStrategy, deployedValue, isProtected, openedToday, periodRealized, periodRealizedPartial, periodUnclaimedRealized, readDayMove, rowPeriodRealized, strategyPeriodRealized, securedValue, sessionRealized, visibleBooks } from "./books";
import type { TradeDTO } from "@/lib/api/types";

/** Minimal cycle — only the fields attribution reads. */
function cycle(over: Partial<TradeDTO> & { strategy_id: string }): TradeDTO {
  return {
    instrument_id: "AAA.XNAS",
    realized_pnl: "0.00 USD",
    quantity: 0,
    side: "LONG",
    state: "CLOSED",
    is_engaged: true,
    is_capital_deployed: false,
    ...over,
  } as unknown as TradeDTO;
}

const ext = (over: Record<string, unknown> = {}) =>
  ({ source: "POSITION", strategy_id: "EXTERNAL", realized_pnl: "0.00 USD", ...over }) as never;

const bookFor = (a: ReturnType<typeof attribute>, id: string) =>
  a.strategies.find((s) => s.strategyId === id)?.book;

describe("attribute", () => {
  it("gives every StrategyId its own book, discovered from the data", () => {
    const a = attribute(
      [
        cycle({ strategy_id: "MOMENTUM-002", realized_pnl: "-1289.79 USD" }),
        cycle({ strategy_id: "MANUAL-001", realized_pnl: "-481.32 USD" }),
        cycle({ strategy_id: "ETF_AUTO-001", realized_pnl: "12.00 USD" }),
      ],
      [],
    );
    expect(a.strategies).toHaveLength(3);
    expect(bookFor(a, "MOMENTUM-002")!.realized).toBeCloseTo(-1289.79);
    expect(bookFor(a, "MANUAL-001")!.realized).toBeCloseTo(-481.32);
    expect(bookFor(a, "ETF_AUTO-001")!.realized).toBe(12);
  });

  it("does not privilege MANUAL — it is a strategy like any other", () => {
    // An earlier version special-cased MANUAL into a "discretionary" bucket, which baked in a
    // two-book world the architecture does not have.
    const a = attribute([cycle({ strategy_id: "MANUAL-001", realized_pnl: "5.00 USD" })], []);
    expect(a.strategies).toEqual([expect.objectContaining({ strategyId: "MANUAL-001" })]);
    expect(a.unclaimed.total).toBe(0);
  });

  it("picks up a strategy nobody hardcoded", () => {
    const a = attribute([cycle({ strategy_id: "SOMETHING-NEW-007", realized_pnl: "3.00 USD" })], []);
    expect(bookFor(a, "SOMETHING-NEW-007")!.realized).toBe(3);
  });

  it("keeps unclaimed positions out of every strategy book", () => {
    // Folding these into MANUAL would charge a strategy for a trade a human made at the broker.
    const a = attribute([cycle({ strategy_id: "MANUAL-001", realized_pnl: "10.00 USD" })], [
      ext({ realized_pnl: "33.92 USD", unrealized_pl: 5 }),
    ]);
    expect(bookFor(a, "MANUAL-001")!.total).toBe(10);
    expect(a.unclaimed.total).toBeCloseTo(38.92);
    expect(a.unclaimed.held).toBe(1);
  });

  it("splits one instrument held by two strategies — why this works on cycles, not groups", () => {
    // groupByInstrument merges these into ONE row for the net anchor, so a group-level split would
    // hand the whole instrument to whichever strategy was looked at first.
    const a = attribute(
      [
        cycle({ instrument_id: "FIG.XNYS", strategy_id: "MOMENTUM-002", realized_pnl: "100.00 USD" }),
        cycle({ instrument_id: "FIG.XNYS", strategy_id: "MANUAL-001", realized_pnl: "-73.87 USD" }),
      ],
      [],
    );
    expect(bookFor(a, "MOMENTUM-002")!.realized).toBe(100);
    expect(bookFor(a, "MANUAL-001")!.realized).toBeCloseTo(-73.87);
  });

  it("only counts unrealized for deployed cycles, and flags held positions with no mark", () => {
    const a = attribute(
      [
        cycle({ strategy_id: "M-1", is_capital_deployed: true, quantity: 10, unrealized_pl: 50 } as never),
        cycle({ strategy_id: "M-1", is_capital_deployed: true, quantity: 10 }), // no mark yet
        cycle({ strategy_id: "M-1", realized_pnl: "7.00 USD" }), // flat — no unrealized
      ],
      [],
    );
    const b = bookFor(a, "M-1")!;
    expect(b.unrealized).toBe(50);
    expect(b.unknown).toBe(1);
    expect(b.held).toBe(2);
    expect(b.total).toBe(57);
  });

  it("orders most-held first, then by size of P&L, then by id so ties never jitter", () => {
    const a = attribute(
      [
        cycle({ strategy_id: "B-1", realized_pnl: "5.00 USD" }),
        cycle({ strategy_id: "A-1", realized_pnl: "5.00 USD" }),
        cycle({ strategy_id: "C-1", is_capital_deployed: true, quantity: 1, unrealized_pl: 1 } as never),
      ],
      [],
    );
    expect(a.strategies.map((s) => s.strategyId)).toEqual(["C-1", "A-1", "B-1"]);
  });

  it("survives a malformed Money string rather than poisoning a book with NaN", () => {
    const a = attribute([cycle({ strategy_id: "M-1", realized_pnl: "oops" })], []);
    expect(bookFor(a, "M-1")!.total).toBe(0);
  });
});

describe("visibleBooks", () => {
  it("drops silent books and puts unclaimed last", () => {
    const rows = visibleBooks(
      attribute(
        [
          cycle({ strategy_id: "MOMENTUM-002", is_capital_deployed: true, quantity: 5, unrealized_pl: 9 } as never),
          cycle({ strategy_id: "MANUAL-001", realized_pnl: "2.00 USD" }),
          cycle({ strategy_id: "IDLE-001" }), // nothing held, nothing realized
        ],
        [ext({ realized_pnl: "1.00 USD" })],
      ),
    );
    expect(rows.map((r) => r.label)).toEqual(["MOMENTUM-002", "MANUAL-001", "Unclaimed"]);
  });

  it("returns nothing when only one book would show — it would restate the net above it", () => {
    expect(visibleBooks(attribute([cycle({ strategy_id: "MOMENTUM-002", realized_pnl: "5.00 USD" })], []))).toEqual([]);
  });
});

describe("dayMove", () => {
  const held = (over: Record<string, unknown>) =>
    cycle({ strategy_id: "M-1", is_capital_deployed: true, instrument_id: "AAA.XNAS", ...over } as never);

  it("measures from the PRIOR CLOSE, not from entry — this is the day's move, not the trade's P&L", () => {
    // Entered at 90 today, prior close 100, now 104: the NAME is up 4 on the day even though the
    // trade is up 14. Day change means the same thing here as on the watchlist.
    const d = dayMove([held({ quantity: 10, avg_px_open: 90, last_px: 104 })], new Map([["AAA.XNAS", 100]]));
    expect(d.value).toBe(40);
    expect(d.covered).toBe(1);
  });

  it("signs the quantity so a short gains when the price falls", () => {
    const d = dayMove(
      [held({ side: "SHORT", quantity: -10, last_px: 95 })],
      new Map([["AAA.XNAS", 100]]),
    );
    expect(d.value).toBe(50);
  });

  it("excludes and COUNTS a holding with no prior close rather than calling it flat", () => {
    const d = dayMove([held({ quantity: 10, last_px: 50 })], new Map());
    expect(d.value).toBe(0);
    expect(d.missing).toBe(1);
    expect(d.covered).toBe(0); // 0 covered → the value means nothing, and the tile says so
  });

  it("ignores cycles that are not deployed — a flat name did not move the book today", () => {
    const d = dayMove([cycle({ strategy_id: "M-1", last_px: 50 } as never)], new Map([["AAA.XNAS", 40]]));
    expect(d.covered).toBe(0);
  });

  it("splits the day by strategy", () => {
    const m = dayMoveByStrategy(
      [
        held({ strategy_id: "MOMENTUM-002", quantity: 10, last_px: 110 }),
        held({ strategy_id: "MANUAL-001", quantity: 5, last_px: 110 }),
      ],
      new Map([["AAA.XNAS", 100]]),
    );
    expect(m.get("MOMENTUM-002")!.value).toBe(100);
    expect(m.get("MANUAL-001")!.value).toBe(50);
  });
});

describe("securedValue", () => {
  const held = (over: Record<string, unknown>) =>
    cycle({ strategy_id: "M-1", is_capital_deployed: true, quantity: 100, avg_px_open: 10, ...over } as never);

  it("counts the gain a protective stop above entry would realise", () => {
    const s = securedValue([
      held({ working_orders: [{ side: "SELL", order_type: "STOP_MARKET", leaves_qty: 100, trigger_price: 12 }] }),
    ]);
    expect(s.value).toBe(200); // 100 × (12 − 10)
    expect(s.covered).toBe(1);
    expect(s.naked).toBe(0);
  });

  it("secures nothing when the stop sits BELOW entry — that is loss limitation, not a locked-in gain", () => {
    const s = securedValue([
      held({ working_orders: [{ side: "SELL", order_type: "STOP_MARKET", leaves_qty: 100, trigger_price: 8 }] }),
    ]);
    expect(s.value).toBe(0);
    expect(s.covered).toBe(1); // still protected, just not in the money
  });

  it("ignores a resting LIMIT — a target secures nothing, the price can fall through entry first", () => {
    const s = securedValue([
      held({ working_orders: [{ side: "SELL", order_type: "LIMIT", leaves_qty: 100, price: 15 }] }),
    ]);
    expect(s.value).toBe(0);
    expect(s.naked).toBe(1);
  });

  it("only secures the quantity the stop actually covers", () => {
    const s = securedValue([
      held({ working_orders: [{ side: "SELL", order_type: "STOP_MARKET", leaves_qty: 40, trigger_price: 12 }] }),
    ]);
    expect(s.value).toBe(80); // 40 × 2, not 100 × 2
  });

  it("handles a short: secured when the stop is BELOW entry", () => {
    const s = securedValue([
      held({ side: "SHORT", quantity: -50, avg_px_open: 20, working_orders: [{ side: "BUY", order_type: "STOP_MARKET", leaves_qty: 50, trigger_price: 18 }] }),
    ]);
    expect(s.value).toBe(100); // 50 × (20 − 18)
  });

  it("does not treat a same-direction order as protection", () => {
    // A BUY resting under a LONG is an add, not a stop.
    const s = securedValue([
      held({ working_orders: [{ side: "BUY", order_type: "STOP_MARKET", leaves_qty: 100, trigger_price: 12 }] }),
    ]);
    expect(s.naked).toBe(1);
    expect(s.value).toBe(0);
  });

  it("counts unprotected holdings — the live case tonight, 8 held and nothing resting", () => {
    const s = securedValue([held({ working_orders: [] }), held({}), held({ working_orders: null })]);
    expect(s.value).toBe(0);
    expect(s.naked).toBe(3);
    expect(s.covered).toBe(0);
  });

  it("ignores cycles that are engaged but not deployed", () => {
    const s = securedValue([cycle({ strategy_id: "M-1", state: "ARMED" })]);
    expect(s.naked).toBe(0);
  });
});

describe("deployedValue", () => {
  it("prefers the DTO's own market_value over recomputing it", () => {
    // The engine marks this server-side. Recomputing |qty| × last_px would be a second answer to a
    // question the plane already answers — here they disagree on purpose to prove which one wins.
    const d = deployedValue(
      [cycle({ strategy_id: "M-1", is_capital_deployed: true, quantity: 54, last_px: 1, market_value: 9873.36 } as never)],
      [ext({ market_value: 5962.14 })],
    );
    expect(d.value).toBeCloseTo(9873.36 + 5962.14);
    expect(d.unmarked).toBe(0);
  });

  it("falls back to |qty| × last only when market_value is absent", () => {
    const d = deployedValue(
      [cycle({ strategy_id: "M-1", is_capital_deployed: true, quantity: 54, last_px: 182.84 } as never)],
      [],
    );
    expect(d.value).toBeCloseTo(54 * 182.84);
  });

  it("takes |market_value| so a short adds to deployed rather than netting against it", () => {
    const d = deployedValue(
      [cycle({ strategy_id: "M-1", is_capital_deployed: true, side: "SHORT", quantity: -10, market_value: -200 } as never)],
      [],
    );
    expect(d.value).toBe(200);
  });

  it("ignores cycles that are engaged but not deployed — an ARMED qty-0 name is not capital at risk", () => {
    const d = deployedValue([cycle({ strategy_id: "M-1", state: "ARMED", quantity: 0, last_px: 50 } as never)], []);
    expect(d.value).toBe(0);
  });

  it("counts an unmarked holding instead of guessing its worth", () => {
    // Contributing 0 understates deployed capital, which makes the book look safer than it is —
    // so the count is surfaced and the tile renders a "+".
    const d = deployedValue([cycle({ strategy_id: "M-1", is_capital_deployed: true, quantity: 10 })], [
      ext({ market_value: null }),
    ]);
    expect(d.value).toBe(0);
    expect(d.unmarked).toBe(2);
  });
});

// --- day move for a position opened TODAY (Operator, 2026-08-13: "the 995 manual day looks wrong") -------

/** 2026-08-12 14:00 ET, in epoch ms — a real session afternoon. */
const AUG12_AFTERNOON_MS = Date.parse("2026-08-12T18:00:00Z");
const NS = 1e6;

function held(over: Partial<TradeDTO> = {}): TradeDTO {
  return {
    instrument_id: "NBIS.XNAS",
    strategy_id: "MANUAL-001",
    side: "LONG",
    quantity: 14,
    is_capital_deployed: true,
    ...over,
  } as TradeDTO;
}

describe("dayMove basis", () => {
  it("measures a position opened TODAY from its entry, not the prior close", () => {
    // The real case. NBIS closed 193.23 -> 250.23 on 2026-08-12, up 29%. The operator bought 14 at 246.91 that
    // afternoon and made ~$46. Measuring from the prior close credited him with 14 x 57.00 = $798, and
    // the Home tile read `day $995.66` against a real $278.66.
    const trades = [
      held({
        last_px: 250.23,
        avg_px_open: 246.91,
        opened_ts: Date.parse("2026-08-12T17:44:49Z") * NS,
      } as Partial<TradeDTO>),
    ];
    const priors = new Map([["NBIS.XNAS", 193.23]]);
    const { value, covered } = dayMove(trades, priors, AUG12_AFTERNOON_MS);
    expect(covered).toBe(1);
    expect(value).toBeCloseTo(14 * (250.23 - 246.91), 2); // ~46.48, NOT ~798
  });

  it("still measures a position held from a PREVIOUS session against the prior close", () => {
    // Unchanged behaviour, and the reason the entry rule has to be conditional: for a position carried
    // overnight the entry is irrelevant to today's move.
    const trades = [
      held({
        instrument_id: "SMH.XNAS",
        quantity: 17,
        last_px: 588.56,
        avg_px_open: 400,
        opened_ts: Date.parse("2026-08-05T14:00:00Z") * NS,
      } as Partial<TradeDTO>),
    ];
    const priors = new Map([["SMH.XNAS", 572.94]]);
    expect(dayMove(trades, priors, AUG12_AFTERNOON_MS).value).toBeCloseTo(17 * (588.56 - 572.94), 2);
  });

  it("falls back to the prior close when a same-day cycle has no entry price", () => {
    const trades = [
      held({ last_px: 250.23, avg_px_open: undefined, opened_ts: Date.parse("2026-08-12T17:44:49Z") * NS } as Partial<TradeDTO>),
    ];
    expect(dayMove(trades, new Map([["NBIS.XNAS", 193.23]]), AUG12_AFTERNOON_MS).value).toBeCloseTo(
      14 * (250.23 - 193.23),
      2,
    );
  });

  it("counts a same-day position as missing only when it has NEITHER basis", () => {
    const trades = [held({ last_px: 250.23, avg_px_open: undefined, opened_ts: undefined } as Partial<TradeDTO>)];
    expect(dayMove(trades, new Map(), AUG12_AFTERNOON_MS).missing).toBe(1);
  });

  it("a short opened today gains when the price falls", () => {
    const trades = [
      held({
        side: "SHORT",
        quantity: 10,
        last_px: 90,
        avg_px_open: 100,
        opened_ts: Date.parse("2026-08-12T15:00:00Z") * NS,
      } as Partial<TradeDTO>),
    ];
    expect(dayMove(trades, new Map(), AUG12_AFTERNOON_MS).value).toBeCloseTo(100, 2);
  });
});

describe("openedToday", () => {
  it("uses the ET session date, not UTC", () => {
    // 2026-08-12 21:00 ET is 2026-08-13 01:00 UTC. Same ET session; a UTC comparison would say no.
    const evening = Date.parse("2026-08-13T01:00:00Z");
    expect(openedToday(Date.parse("2026-08-13T01:30:00Z") * NS, evening)).toBe(true);
  });

  it("is false for a null timestamp", () => {
    expect(openedToday(null, AUG12_AFTERNOON_MS)).toBe(false);
  });
});

describe("sessionRealized (#233)", () => {
  it("prefers the ENGINE's figure — summing live cycles is structurally $0.00 after a close", () => {
    // The live case: four MANUAL positions closed for +483.22 while the tile summed only live cycles
    // and showed nothing, because a CLOSED cycle is emitted once and dropped from the projection.
    const frame = { realized_session: { total: 483.22 } };
    expect(sessionRealized(frame, 0)).toBe(483.22);
  });

  it("falls back to the live-cycle sum when the engine offers no figure", () => {
    // A frame that predates the field, or a node with no engine. Showing the old imperfect number beats
    // a confident $0.00 — zero is a claim, absent is not.
    expect(sessionRealized(undefined, 17.5)).toBe(17.5);
    expect(sessionRealized({}, 17.5)).toBe(17.5);
    expect(sessionRealized({ realized_session: null }, 17.5)).toBe(17.5);
  });

  it("uses a genuine ZERO from the engine rather than falling back", () => {
    // The distinction the fallback must not blur: the engine saying "nothing closed today" is an
    // ANSWER. Treating 0 as absent would resurrect the stale sum on every flat day.
    expect(sessionRealized({ realized_session: { total: 0 } }, 999)).toBe(0);
  });

  it("falls back on a non-finite total rather than rendering NaN", () => {
    expect(sessionRealized({ realized_session: { total: NaN } }, 42)).toBe(42);
  });
});

describe("the BOOK tile actually uses the engine's realized figure (#233)", () => {
  /**
   * The call site, not the helper.
   *
   * Removing the call from `BookTile` and going back to summing live cycles left every test above
   * green — the helper was covered, the wiring was not. That is the third time today the same gap
   * appeared, so it gets the same treatment `mobileWidth.test.ts` uses: assert the invariant across
   * the source, because a rendered assertion cannot reach a component this repo has no harness for.
   *
   * The invariant: whatever computes REALIZED must consult the engine's field. Summing `b.realized`
   * ALONE is the original bug — a CLOSED cycle is dropped from the projection, so that sum is
   * structurally $0.00 the moment a position closes.
   */
  it("BookTile computes realized through the engine's figure, not by summing live cycles alone", () => {
    const source = readFileSync(join(__dirname, "..", "book", "BookTile.tsx"), "utf8");
    const line = source
      .split("\n")
      .find((l) => /const\s+realized\s*=/.test(l));

    expect(line, "BookTile no longer computes `realized` at all").toBeTruthy();
    // `periodRealized` FALLS BACK to `sessionRealized` for 1D, so this still pins the #233 invariant —
    // it now pins the #322 one on top: the figure is scoped to the operator's chosen window.
    expect(line).toMatch(/periodRealized/);
    // The period must be the STORE VALUE, not a literal. `toContain("period")` was satisfied by the
    // substring inside `periodRealizedOf` itself, so hardcoding "1D" passed it — an assertion that
    // cannot fail carries no information.
    expect(line, "REALIZED is pinned to a hardcoded window, ignoring the global selector").toMatch(
      /periodRealizedOf\(\s*\w+\s*,\s*period\s*,/,
    );
  });

  describe("realized answers for the CHOSEN period (#322)", () => {
    const frame = {
      realized_session: { total: 1061.0 },
      realized_periods: { "1D": { total: 1061.0 }, "1W": { total: 2841.5 }, all: { total: 9812.0 } },
    };

    it("uses the swept figure for the selected period", () => {
      expect(periodRealized(frame, "1W", 0)).toBe(2841.5);
      expect(periodRealized(frame, "all", 0)).toBe(9812.0);
    });

    it("an UNSWEPT period is null, NOT zero", () => {
      // The distinction the operator's question turned on. `$0.00` for a month asserts that nothing closed all
      // month; a dash says nobody has asked yet. Rendering the first as the second is how REALIZED read
      // $0.00 through four closed positions worth +$483 (#233).
      expect(periodRealized(frame, "3M", 0)).toBeNull();
      expect(periodRealized(undefined, "1M", 0)).toBeNull();
    });

    it("1D falls back to the native session figure while no sweep has landed", () => {
      // Same window, different source: native lands seconds after a boot, the sweep takes up to 5min.
      const noSweep = { realized_session: { total: 483.22 } };
      expect(periodRealized(noSweep, "1D", 0)).toBe(483.22);
    });

    it("1D falls back again to the live-cycle sum when there is no engine at all", () => {
      expect(periodRealized(undefined, "1D", 77.5)).toBe(77.5);
    });

    it("prefers the SWEPT 1D figure over the session one when both exist", () => {
      // They can disagree: the session figure resets on an engine restart, the sweep does not.
      const both = { realized_session: { total: 0 }, realized_periods: { "1D": { total: 483.22 } } };
      expect(periodRealized(both, "1D", 0)).toBe(483.22);
    });
  });
});

describe("securedValue trusts the BROKER over the cache (#285)", () => {
  it("counts a position the broker is protecting even when the cache shows no orders", () => {
    // The live failure: 8 GTC stops resting at Alpaca, and the tile read "8 of 8 unprotected" because
    // the engine held every one as REJECTED — a submit whose HTTP call failed after the venue accepted
    // it. Nautilus refuses REJECTED -> ACCEPTED, so reconciliation could never repair them.
    const s = securedValue([held({ working_orders: [], broker_protected: true })]);
    expect(s.naked).toBe(0);
    expect(s.covered).toBe(1);
  });

  it("counts a position NAKED when the broker says nothing rests, whatever the cache thinks", () => {
    // The mirror, and why this is not an OR. A stale cached stop must not hide a position the broker is
    // not actually protecting — that is the same false claim in the dangerous direction.
    const s = securedValue([
      held({
        broker_protected: false,
        working_orders: [{ side: "SELL", order_type: "STOP_MARKET", leaves_qty: 100, trigger_price: 12 } as never],
      }),
    ]);
    expect(s.naked).toBe(1);
    expect(s.covered).toBe(0);
  });

  it("falls back to the cache when the broker has not been asked", () => {
    // undefined is "not asked", not "nothing resting". A node with the protection poll off must not
    // declare the whole book naked.
    const s = securedValue([
      held({
        working_orders: [{ side: "SELL", order_type: "STOP_MARKET", leaves_qty: 100, trigger_price: 12 } as never],
      }),
    ]);
    expect(s.covered).toBe(1);
    expect(securedValue([held({ working_orders: [] })]).naked).toBe(1);
  });
});

describe("a period can UNDERSTATE, and must say so (#322)", () => {
  // The live shape on 2026-08-17: a week that reads higher than the account's entire history.
  const frame = {
    realized_periods: {
      "1W": { total: 2195.32, unmatched: 19 },
      all: { total: 107.7, unmatched: 0 },
    },
  };

  it("flags the window whose sells opened before it", () => {
    expect(periodRealizedPartial(frame, "1W")).toEqual({ partial: true, unmatched: 19 });
  });

  it("does not flag a window where every round trip closed inside it", () => {
    expect(periodRealizedPartial(frame, "all")).toEqual({ partial: false, unmatched: 0 });
  });

  it("the FIXTURE actually contains the paradox this exists to explain", () => {
    // Without the shorter window exceeding the longer one, these assertions describe nothing —
    // partiality would be a label on a number nobody would question.
    expect(frame.realized_periods["1W"].total).toBeGreaterThan(frame.realized_periods.all.total);
  });

  it("an unswept or absent period is NOT reported as partial", () => {
    // Unknown and understated are different claims. A dash already says "not swept"; adding a
    // partiality marker to it would assert something about data nobody has fetched.
    expect(periodRealizedPartial(frame, "3M")).toEqual({ partial: false, unmatched: 0 });
    expect(periodRealizedPartial(undefined, "1W")).toEqual({ partial: false, unmatched: 0 });
  });

  it("BookTile renders the marker, not just computes it", () => {
    const source = readFileSync(join(__dirname, "..", "book", "BookTile.tsx"), "utf8");
    expect(source).toContain("periodRealizedPartial(tradesFrame, period)");
    expect(source, "partiality is computed and never shown").toMatch(/partial\.partial \? "\*" : ""/);
    expect(source, "no explanation reaches the operator").toContain("UNDERSTATED");
  });
});

/**
 * #251 — the "unprotected" filter and the SECURED tally must mean the same thing.
 *
 * `isProtected` was extracted FROM `securedValue` rather than reimplemented beside it. The logic is not
 * obvious: broker truth overrides the cache in one direction (a stop the cache holds as REJECTED but the
 * venue reports OPEN — #285, which cost a day of "8 of 8 unprotected" while 8 GTC stops rested) and the
 * cache's silence loses to the broker in the other. A second copy drifts the first time either side
 * changes, and a filter that says "unprotected" while SECURED says covered is a SAFETY claim that is
 * wrong on one of the two screens showing it.
 */
describe("isProtected is the one predicate behind SECURED and the filter (#251)", () => {
  const base = {
    instrument_id: "FSM.XNYS", strategy_id: "MOMENTUM-002", side: "LONG",
    quantity: 100, avg_px_open: 10, is_capital_deployed: true, realized_pnl: "0.00 USD",
  } as unknown as TradeDTO;

  const withStop = (extra: Record<string, unknown>) =>
    ({ ...base, working_orders: [{ order_type: "STOP_MARKET", side: "SELL", status: "ACCEPTED", trigger_price: 9 }], ...extra }) as unknown as TradeDTO;
  const noStop = (extra: Record<string, unknown> = {}) => ({ ...base, working_orders: [], ...extra }) as unknown as TradeDTO;

  it("the fixture can express both answers", () => {
    // Assert the fixture's own property first: if every case agreed, the agreement assertions below
    // would hold against any implementation, including two that disagree elsewhere.
    expect(isProtected(withStop({}))).toBe(true);
    expect(isProtected(noStop())).toBe(false);
  });

  it("a resting stop protects; nothing resting does not", () => {
    expect(isProtected(withStop({}))).toBe(true);
    expect(isProtected(noStop())).toBe(false);
  });

  it("BROKER TRUTH WINS BOTH WAYS — the half that a reimplementation would get wrong", () => {
    // Cache silent, broker says a stop rests: protected. This is #285, and getting it backwards
    // reported 8 of 8 positions unprotected while 8 GTC stops rested at the venue.
    expect(isProtected(noStop({ broker_protected: true }))).toBe(true);
    // Cache holds a stop, broker says nothing rests: NOT protected. Counting this as covered hides a
    // genuinely naked position behind a stale order.
    expect(isProtected(withStop({ broker_protected: false }))).toBe(false);
    // Broker not asked (undefined) is not the same as "nothing resting" — the cache's answer stands.
    expect(isProtected(withStop({ broker_protected: undefined }))).toBe(true);
    expect(isProtected(noStop({ broker_protected: undefined }))).toBe(false);
  });

  it("SECURED's naked count agrees with the predicate on every case", () => {
    // The anti-drift assertion, and the reason this file rather than the tile's. Two derivations of one
    // fact will disagree; pinning that they are the SAME derivation is what stops it.
    const cases = [withStop({}), noStop(), noStop({ broker_protected: true }), withStop({ broker_protected: false })];
    const expectedNaked = cases.filter((t) => !isProtected(t)).length;
    expect(securedValue(cases).naked).toBe(expectedNaked);
    expect(securedValue(cases).covered).toBe(cases.length - expectedNaked);
  });

  it("a cycle with no capital deployed is not counted as naked", () => {
    // SECURED skips it, so the filter must too — otherwise a closed cycle shows up under "Unprotected"
    // forever and the count never reaches zero.
    expect(securedValue([{ ...noStop(), is_capital_deployed: false } as TradeDTO]).naked).toBe(0);
  });
});

/** A protective stop with EVERY field production sends — a thinner double types as valid and is not. */
function stopOrder(trigger: number, qty: number) {
  return {
    client_order_id: `PROT-SELL-TEST-${trigger}`,
    side: "SELL",
    order_type: "STOP_MARKET",
    quantity: qty,
    leaves_qty: qty,
    trigger_price: trigger,
    time_in_force: "GTC",
    status: "ACCEPTED",
    ts_last: 0,
    // Production's every working order carries this (#872) — untagged is `[]`, and an untagged
    // STOP_MARKET is exactly what these fixtures mean: a trailed stop, not an entry floor.
    tags: [] as string[],
  };
}

describe("SECURED $0.00 is ambiguous unless the counts distinguish it (#345)", () => {
  it("separates 'nothing protected' from 'protected, nothing above entry'", () => {
    // Both render $0.00 and they mean opposite things. Live on 2026-08-20 the book was 3 of 3 protected
    // with secured $0.00 — every stop resting, none above its cost — and the hero cell showed a bare zero
    // that reads as "nothing is protected". `covered`/`naked` are what let the sub-line say which, and
    // they are already returned; the tile simply was not rendering them in this case.
    const stopBelowEntry = securedValue([
      held({ avg_px_open: 100, quantity: 10, broker_protected: true,
             working_orders: [stopOrder(95, 10)] }),
    ]);
    expect(stopBelowEntry.value).toBe(0);
    expect(stopBelowEntry.covered).toBe(1);
    expect(stopBelowEntry.naked).toBe(0);

    const noStopAtAll = securedValue([
      held({ avg_px_open: 100, quantity: 10, broker_protected: false, working_orders: [] }),
    ]);
    expect(noStopAtAll.value).toBe(0);
    expect(noStopAtAll.covered).toBe(0);
    expect(noStopAtAll.naked).toBe(1);

    // The value alone cannot tell them apart — which is the whole point of the sub-line.
    expect(stopBelowEntry.value).toBe(noStopAtAll.value);
  });
})

describe("the strategy cells must answer the selected window (#345 item 3)", () => {
  it("Portfolio reads the period and the per-strategy day move", () => {
    // Operator, on a 1D tab: "I have selected 1d. so I want to see 1 day." `rows.map` never read `period`,
    // so the cells rendered lifetime unrealized on open cycles under EVERY tab — the same number at 1D,
    // 1W, 1M and All, with nothing saying which window it was.
    //
    // Asserted on source because this repo renders nothing in a test. The property is that the tile
    // CONSUMES the selector at all; before this it did not reference it.
    const src = readFileSync(
      join(import.meta.dirname, "ManagedPortfolioTile.tsx"),
      "utf8",
    );
    const code = src.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
    expect(code).toMatch(/period\s*=\s*useCockpitStore/);
    expect(code).toMatch(/dayMoveByStrategy\(/);
    expect(code).toMatch(/period=\{period\}/);
  });

  it("uses the SAME day-move function Home uses, not a second one", () => {
    // Two derivations of "today's move" would let Portfolio and Home disagree about the same position.
    // #270 already fixed exactly that once, when this tile computed `netQty × (last − priorClose)`
    // itself and diverged from Home on a name bought part-way through a move.
    const src = readFileSync(join(import.meta.dirname, "ManagedPortfolioTile.tsx"), "utf8");
    const code = src.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
    expect(code).toMatch(/from "\.\/books"/);
    expect(code).not.toMatch(/last\s*-\s*priorClose\s*\)\s*\*/); // no hand-rolled day maths
  });
})

// ==================================================================================================
// #345 item 3 — the strategy cells showed a SESSION figure and ignored the period selector.
//
// `Book.realized` sums `realized_pnl` off the LIVE cycles. Nautilus's cache holds positions closed
// during this process's life, and reconciliation restores OPEN positions on startup, not closed ones —
// so after any restart every strategy cell reads `real $0.00`. Proof off the 2026-08-18 screen:
//
//     MOMENTUM-002 1,245.59 + MANUAL-001 (-68.80) = 1,176.79 = UNREALIZED, exactly
//
// Both cells printed $0.00 while the book had realized $2,650.21 over 1W. And `rows.map` never read
// `period`, so the cells could not respond to the selector at all.
//
// Attribution follows #292, decided by the operator 2026-08-14: P&L goes to the strategy that OPENED the lot,
// FIFO, and the closer takes none of it. The engine does that; these accessors only read it.
// ==================================================================================================

describe("per-strategy realized reads the swept window, not the session (#345 item 3)", () => {
  const frame = {
    realized_periods: {
      "1D": { total: -226.2, by_strategy: { "MANUAL-001": -144.58, "MOMENTUM-002": -81.62 }, unclaimed: 0 },
      // The FULL measured breakdown. An earlier version of this fixture listed only MANUAL-001 and
      // failed its own invariant test by $693.75 — a fixture that cannot represent the engine's output
      // is the same defect as a double that cannot represent production.
      all: {
        total: 3574.04,
        by_strategy: { "MANUAL-001": -376.37, "MOMENTUM-002": -12.68, EXTERNAL: -681.07 },
        unclaimed: 4644.16,
      },
    },
  };

  it("the fixture carries the real measured numbers", () => {
    // The fixture's own property first: 1D must be FULLY attributed and `all` must NOT be, or the
    // unclaimed assertions below prove nothing. Both figures are from the live account 2026-08-21.
    expect(frame.realized_periods["1D"].unclaimed).toBe(0);
    expect(frame.realized_periods.all.unclaimed).toBeGreaterThan(0);
  });

  it("returns the strategy's own figure for the selected window", () => {
    expect(strategyPeriodRealized(frame, "1D", "MOMENTUM-002")).toBe(-81.62);
    expect(strategyPeriodRealized(frame, "1D", "MANUAL-001")).toBe(-144.58);
  });

  it("the window CHANGES the answer — this is the whole defect", () => {
    // One strategy, two windows, two different numbers. The shipped cell showed one figure under
    // every tab because `rows.map` never read `period`.
    expect(strategyPeriodRealized(frame, "1D", "MANUAL-001")).not.toBe(
      strategyPeriodRealized(frame, "all", "MANUAL-001"),
    );
  });

  it("a swept window with no entry for a strategy is a genuine ZERO", () => {
    // The window was swept and this strategy simply did not realize anything in it. That is 0, not
    // unknown, and rendering it as a dash would understate certainty. BCTROT-004 is the real case:
    // it had 16 fills on 2026-08-20 and no closed round trip, so it is absent from every bucket.
    expect(strategyPeriodRealized(frame, "all", "BCTROT-004")).toBe(0);
    expect(strategyPeriodRealized(frame, "1D", "BCTROT-004")).toBe(0);
  });

  it("an UNSWEPT window is null, which is not zero", () => {
    // The distinction this codebase keeps having to relearn (#343, #370, #298): unknown is an answer,
    // and a number known to be absent must not render as a number.
    expect(strategyPeriodRealized(frame, "3M", "MANUAL-001")).toBeNull();
    expect(strategyPeriodRealized(undefined, "1D", "MANUAL-001")).toBeNull();
    expect(strategyPeriodRealized({ realized_periods: null }, "1D", "MANUAL-001")).toBeNull();
  });

  it("surfaces the UNATTRIBUTED remainder rather than hiding it", () => {
    // On `all` the unattributed share is LARGER than everything attributed — the cache does not reach
    // past 2026-08-17. A breakdown that omitted it would look complete while covering a fraction of
    // the money, and per #292 it must not be handed to whoever closed the position.
    expect(periodUnclaimedRealized(frame, "all")).toBe(4644.16);
    expect(periodUnclaimedRealized(frame, "1D")).toBe(0);
    expect(periodUnclaimedRealized(frame, "3M")).toBeNull();
  });

  it("the invariant the engine publishes against holds on this fixture", () => {
    // Sum(by_strategy) + unclaimed === total. Two derivations of one fact, so a disagreement is the
    // detector — asserted here so a fixture that stops representing the engine is caught too.
    for (const key of ["1D", "all"] as const) {
      const p = frame.realized_periods[key];
      const recombined = Object.values(p.by_strategy).reduce((a, b) => a + b, 0) + p.unclaimed;
      expect(Math.abs(recombined - p.total)).toBeLessThan(0.01);
    }
  });
});

describe("the tile actually passes the period down (#345 item 3)", () => {
  const SRC = readFileSync(join(import.meta.dirname, "..", "book", "BookTile.tsx"), "utf8");
  const code = SRC.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

  it("the scan reads the real component", () => {
    expect(SRC.length).toBeGreaterThan(1000);
    expect(code).toContain("BookCell");
  });

  it("rows.map reads `period` — it never did before", () => {
    // Now via `rowPeriodRealized`, the ONE derivation both panels call (#523). This used to match the
    // inline `strategyPeriodRealized(...)` ternary; that ternary moved into `books.ts` unchanged so
    // ManagedPortfolioTile could call the same thing instead of growing a second copy. The property
    // under guard is unchanged — `period` and `r.label` still reach the cell — and the routing the
    // ternary performed is pinned directly below, on the helper itself.
    expect(code).toMatch(/rowPeriodRealized\(tradesFrame,\s*period,\s*r\.label\)/);
  });

  it("the cell no longer renders the session figure", () => {
    // Banned by name. `book.realized` is the session sum and is exactly what read $0.00 after a
    // restart; a revert to it must be loud rather than quiet.
    expect(code).not.toMatch(/real \{fmtUsd\(book\.realized\)\}/);
  });

  it("names the window beside the number", () => {
    // An unlabelled figure under a period selector is what #336 and #345 are both about.
    //
    // Asserts the RENDER, not the identifier. A first version matched /periodLabel/ and passed with the
    // rendered label deleted, because `periodLabel` is also a prop name and a type field — the mutation
    // harness caught it. Both the per-strategy figure and the unattributed line must name their window.
    //
    // MATCHES BOTH INTERPOLATION FORMS. #699 moved one of these into a template literal
    // (`real · ${periodLabel}`) and this test went red while the property it names stayed TRUE — a
    // guard recognising its subject by a shape the change happens to alter, which is the class this
    // repo keeps filing. The property is "the window is named beside the figure", not "it is written
    // as JSX interpolation".
    const rendered = code.match(/·\s*\$?\{periodLabel\}/g) ?? [];
    expect(rendered.length).toBeGreaterThanOrEqual(2);
  });

  it("declares by_strategy and unclaimed on the frame type", () => {
    // The TypeScript form of the Pydantic-drops-a-field defect (#233 / #322 / #336): the engine
    // publishes it, the local declaration omits it, the tile reads undefined forever, everything
    // compiles.
    expect(code).toMatch(/by_strategy\?:/);
    expect(code).toMatch(/unclaimed\?:/);
  });
});


// ==================================================================================================
// #345 item 6 — neither strategy cell rendered its day line.
//
// `BookCell` gated on `day.covered > 0` and rendered NOTHING otherwise. Three different situations
// therefore looked identical on screen, and only one of them is benign:
//
//     covered > 0                -> a real figure
//     covered 0, missing > 0     -> positions held, prior closes never arrived   <- A DEAD FEED
//     covered 0, missing 0       -> the strategy holds nothing                   <- benign
//
// Prior closes ride the shared `today_ranges` websocket plane, and the engine TOMBSTONES a symbol whose
// snapshot went stale (`high: null`) so a day change is never computed against the wrong session. When
// that plane is empty or fully tombstoned, `covered` is 0 for EVERY strategy at once and the line
// disappears from the whole panel.
//
// Measured 2026-08-21: all 16 held cycles had a mark and none had a prior close from that plane.
//
// This is #298's failure — the engine had to learn to report which KIND of empty it was — and on
// 2026-08-14 an empty tile stood in for eight held positions. A number that cannot be computed must say
// so; it must not quietly not be there.
// ==================================================================================================

describe("a strategy's day line says WHICH kind of empty it is (#345 item 6)", () => {
  it("the fixture distinguishes the two zeros", () => {
    // The fixture's own property first: both cases have covered === 0, so a test that only checked
    // `covered` could not tell them apart — which is precisely the shipped bug.
    const deadFeed = { value: 0, missing: 8, covered: 0 };
    const flat = { value: 0, missing: 0, covered: 0 };
    expect(deadFeed.covered).toBe(flat.covered);
    expect(deadFeed.missing).not.toBe(flat.missing);
  });

  it("a real figure reads as a value", () => {
    expect(readDayMove({ value: -51.4, missing: 0, covered: 8 })).toEqual({
      kind: "value",
      value: -51.4,
      missing: 0,
    });
  });

  it("a figure computed from SOME positions is still a value, and carries the shortfall", () => {
    // Partial is not unavailable. The number is real, it is just understated, and the cell marks it.
    expect(readDayMove({ value: 12.5, missing: 3, covered: 5 })).toEqual({
      kind: "value",
      value: 12.5,
      missing: 3,
    });
  });

  it("HELD POSITIONS WITH NO PRIOR CLOSE are 'unavailable', never silence", () => {
    // The case the bug hid. Eight positions held, nothing computable — that is a feed gap and the
    // operator has to be told, because the alternative reading is "today was flat".
    expect(readDayMove({ value: 0, missing: 8, covered: 0 })).toEqual({ kind: "unavailable", missing: 8 });
  });

  it("a strategy holding NOTHING renders nothing — that is the benign zero", () => {
    // The cell already says "0 held". A day line here would be noise, and treating this as a feed
    // warning would be crying wolf on healthy state — which this repo has shipped before (#387, #390).
    expect(readDayMove({ value: 0, missing: 0, covered: 0 })).toEqual({ kind: "nothing" });
  });

  it("no DayMove at all is 'nothing', not a crash and not a zero", () => {
    expect(readDayMove(undefined)).toEqual({ kind: "nothing" });
    expect(readDayMove(null)).toEqual({ kind: "nothing" });
  });

  it("a genuinely flat but COVERED day still shows $0.00", () => {
    // The opposite direction: 0.00 with coverage is a real answer and must not be suppressed as
    // "nothing". Collapsing it would reintroduce the ambiguity from the other side.
    expect(readDayMove({ value: 0, missing: 0, covered: 4 })).toEqual({
      kind: "value",
      value: 0,
      missing: 0,
    });
  });
});

// THE NAME PROMISES MORE THAN THE CELL DOES IN ONE STATE (#699 review). All three readings still
// render — value, unavailable, nothing — at every window EXCEPT 1D-covered-with-nothing-missing,
// where the headline already IS the day move and repeating it below was pure duplication. The
// readings themselves are unchanged; only that one redundant render is suppressed. Kept the name
// because the #345 property it guards is the same one.
describe("the cell renders all three day readings (#345 item 6)", () => {
  const SRC = readFileSync(join(import.meta.dirname, "..", "book", "BookTile.tsx"), "utf8");
  const code = SRC.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

  it("the scan reads the real component", () => {
    expect(SRC.length).toBeGreaterThan(1000);
    expect(code).toContain("BookCell");
  });

  it("uses the reading rather than gating on covered", () => {
    // THE MECHANISM MOVED IN #699, THE PROPERTY DID NOT. BookTile used to call `readDayMove(day)`
    // itself; the reading now arrives on `cellHeadline`'s return so that BOTH tiles render this
    // state from ONE derivation — ManagedPortfolioTile previously could not render it at all, which
    // is the same two-copies-of-one-rule shape #523 cost three defects.
    //
    // So this pins that the cell CONSUMES a reading, by either route, and still never reintroduces
    // the gate that produced the silence.
    //
    // NOT `cellHeadline\(` AS AN ALTERNATIVE. Review's catch: that clause is satisfied by any tile
    // that merely CALLS the helper while rendering no day line at all — it would have passed the
    // very removal that shipped silently in 3d80baf. The rendered tests carry this property now;
    // this scan pins only that a reading is CONSUMED, so it must name something that is consumed.
    expect(code).toMatch(/readDayMove\(day\)|head\.day\.kind/);
    // Banned by name: this is the gate that produced the silence.
    expect(code).not.toMatch(/day\.covered > 0/);
  });

  it("says out loud that the prior close is missing", () => {
    expect(code).toMatch(/no prior close/);
  });

  it("the OTHER tile says it too — one derivation, both screens (#699)", () => {
    // ManagedPortfolioTile is the DEFAULT portfolio view and rendered NOTHING in this state: the
    // helper computed the reading and discarded it, so only BookTile — which re-derived it — could
    // warn. A reader on the default screen saw an ordinary-looking cell during a feed outage.
    //
    // STRIPS COMMENTS, like its siblings two lines up, and that is the whole point. Reading the file
    // RAW, this test SURVIVED deletion of the entire warning block: `/no prior close/` matched my own
    // explanatory COMMENT about the "+" marker, and `/text-status-watch/` matched the ARMED badge
    // colour and two unrelated uses. It recognised its subject by phrases the defect does not
    // destroy — asymmetric rigor against the BookTile scans in the same describe, which strip.
    //
    // The rendered tests are what actually carry this property; this scan is the cheap cross-check
    // and it has to be able to fail.
    const raw = readFileSync(join(import.meta.dirname, "ManagedPortfolioTile.tsx"), "utf8");
    const mp = raw.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
    expect(mp).toMatch(/no prior close/);
    // Scoped to the warning itself: `text-status-watch` alone also names the ARMED badge.
    expect(mp).toMatch(/text-status-watch[\s\S]{0,400}no prior close/);
  });

  it("marks the missing-feed case as a WARNING tone, not as ordinary text", () => {
    // A feed gap in a P&L slot is not neutral information. `status-watch` is the tone the rest of the
    // app uses for "this needs your attention but nothing is broken yet".
    expect(code).toMatch(/text-status-watch/);
  });
});

describe("visibleBooks — a strategy that is FLAT still has P&L (#523)", () => {
  // WHAT HAPPENED, 2026-08-24. The per-strategy panel listed MOMENTUM-002, BCTROT-004 and
  // TECHIVOL-005 and omitted MANUAL-001 entirely — while the engine's own sweep reported
  // MANUAL-001 at +1522.07 over 1M, the LARGEST realized contributor in the book.
  //
  // The card list is built from TRADE CYCLES, and BookTile:206 already records why that is not the
  // same set: "A CLOSED cycle is emitted once and dropped from the projection, so `allBooks` only
  // ever contains live ones." That was fixed for the TOTAL by taking the swept figure from the
  // engine. It was never fixed for the ROW LIST, so a strategy holding nothing has no row to put its
  // swept realized in and vanishes.
  //
  // Measured on the live paper stack:
  //   /trades  by strategy -> MOMENTUM-002, QC345-003, BCTROT-004, TECHIVOL-005   (no MANUAL)
  //   realized_periods 1M  -> MANUAL-001 1522.07, MOMENTUM-002 3001.71, BCTROT-004 -126.40,
  //                           EXTERNAL -1093.30
  // Two derivations of "which strategies have P&L", and the panel rendered the INTERSECTION.

  it("lists a strategy that has swept realized but no live cycle", () => {
    const rows = visibleBooks(
      attribute([cycle({ strategy_id: "MOMENTUM-002", realized_pnl: "5.00 USD", is_capital_deployed: true })], []),
      { "MOMENTUM-002": 5, "MANUAL-001": 1522.07 },
    );
    expect(rows.map((r) => r.label)).toContain("MANUAL-001");
  });

  it("gives the flat strategy an EMPTY book, not a fabricated one", () => {
    // The row exists so its swept realized has somewhere to render. Its held/unrealized are genuinely
    // zero — inventing them would make the panel's own total disagree with itself.
    const rows = visibleBooks(
      attribute([cycle({ strategy_id: "MOMENTUM-002", realized_pnl: "5.00 USD", is_capital_deployed: true })], []),
      { "MOMENTUM-002": 5, "MANUAL-001": 1522.07 },
    );
    const manual = rows.find((r) => r.label === "MANUAL-001")!;
    expect(manual.book.held).toBe(0);
    expect(manual.book.unrealized).toBe(0);
  });

  it("does not duplicate a strategy that has BOTH a live cycle and swept realized", () => {
    // TWO strategies on purpose: `visibleBooks` returns [] for a single row (a one-row split is not
    // a split), so a one-strategy fixture would pass this assertion vacuously with zero rows.
    const rows = visibleBooks(
      attribute(
        [
          cycle({ strategy_id: "MOMENTUM-002", realized_pnl: "5.00 USD", is_capital_deployed: true }),
          cycle({ strategy_id: "BCTROT-004", realized_pnl: "3.00 USD", is_capital_deployed: true }),
        ],
        [],
      ),
      { "MOMENTUM-002": 5, "BCTROT-004": 3 },
    );
    expect(rows.length).toBeGreaterThan(1);
    expect(rows.filter((r) => r.label === "MOMENTUM-002")).toHaveLength(1);
  });

  it("ignores a swept strategy whose period realized is zero", () => {
    // Zero is not a contribution. A row per strategy that has ever existed would bury the ones that
    // moved — the panel is read at a glance.
    const rows = visibleBooks(
      attribute([cycle({ strategy_id: "MOMENTUM-002", realized_pnl: "5.00 USD", is_capital_deployed: true })], []),
      { "MOMENTUM-002": 5, "QC345-003": 0 },
    );
    expect(rows.map((r) => r.label)).not.toContain("QC345-003");
  });

  it("does not give EXTERNAL a strategy row — it already has Unclaimed", () => {
    // EXTERNAL appears in the sweep (live 1M: -1093.30) but is NOT a cockpit strategy. It is already
    // represented by the "Unclaimed" row, and a second line for the same money would double-count it
    // to the reader. Written because the mutation that removed this exclusion SURVIVED the suite —
    // the guard was there and nothing checked it.
    const rows = visibleBooks(
      attribute(
        [
          cycle({ strategy_id: "MOMENTUM-002", realized_pnl: "5.00 USD", is_capital_deployed: true }),
          cycle({ strategy_id: "BCTROT-004", realized_pnl: "3.00 USD", is_capital_deployed: true }),
        ],
        [],
      ),
      { "MOMENTUM-002": 5, EXTERNAL: -1093.3 },
    );
    expect(rows.map((r) => r.label)).not.toContain("EXTERNAL");
  });

  it("still returns nothing when there is only one row to show", () => {
    // Pre-existing rule: a one-row split is not a split. Preserved so this change cannot make the
    // panel appear on a single-strategy book.
    expect(
      visibleBooks(attribute([cycle({ strategy_id: "MOMENTUM-002", realized_pnl: "5.00 USD" })], []), {}),
    ).toEqual([]);
  });

  it("behaves exactly as before when no sweep is supplied", () => {
    // The argument is optional: every existing caller passes nothing and must be unaffected.
    const a = attribute([cycle({ strategy_id: "MOMENTUM-002", realized_pnl: "5.00 USD", is_capital_deployed: true })], []);
    expect(visibleBooks(a)).toEqual(visibleBooks(a, undefined));
  });
});

describe("rowPeriodRealized — ONE derivation for both panels (#523)", () => {
  // The ternary this replaces lived inline in BookTile and had no equivalent in ManagedPortfolioTile,
  // so the two panels answered "what did this row realize this window" differently — one of them by
  // not answering at all. Pinned here, on the helper, because that is now the only copy.
  //
  // Numbers are the live paper sweep, 2026-08-27/29.
  const frame = {
    realized_periods: {
      // THE WHOLE map, not a convenient slice. An earlier version of this fixture kept three of the
      // six strategies and the sum invariant below could not hold — the fixture, not the code, was
      // wrong. A double that cannot satisfy the engine's own published invariant is the bug.
      all: {
        total: 3278.71,
        by_strategy: {
          "MOMENTUM-002": 3085.56,
          "MANUAL-001": 1509.72,
          EXTERNAL: -1093.3,
          "BCTROT-004": -120.02,
          "TECHIVOL-005": -57.47,
          "QC345-003": -45.92,
        },
        unclaimed: 0.14,
      },
      "1D": { total: 0, by_strategy: {}, unclaimed: 0 },
    },
  };

  it("the fixture's EXTERNAL and residual are DIFFERENT non-zero quantities", () => {
    // If either were zero, or they were equal, an implementation returning only one of them would
    // still produce the right answer and the assertion below could not fail.
    const row = frame.realized_periods.all;
    expect(row.by_strategy.EXTERNAL).not.toBe(0);
    expect(row.unclaimed).not.toBe(0);
    expect(row.by_strategy.EXTERNAL).not.toBeCloseTo(row.unclaimed, 4);
  });

  it("routes a STRATEGY label to its own swept figure", () => {
    expect(rowPeriodRealized(frame, "all", "MANUAL-001")).toBeCloseTo(1509.72, 2);
  });

  it("routes the Unclaimed row to EXTERNAL **plus** the residual — not to its own label", () => {
    // #596: `by_strategy["Unclaimed"]` does not exist, so a plain lookup returned the "genuine zero"
    // branch and -1,093.30 vanished from a panel whose job is to add up. BOTH terms, not either.
    expect(rowPeriodRealized(frame, "all", "Unclaimed")).toBeCloseTo(-1093.3 + 0.14, 2);
  });

  it("the rows it produces sum to the engine's own total", () => {
    // The invariant the whole panel exists to satisfy: Sum(strategy rows) + Unclaimed === total.
    const strategies = ["MOMENTUM-002", "MANUAL-001", "BCTROT-004", "TECHIVOL-005", "QC345-003"];
    const sum =
      strategies.reduce((a, id) => a + (rowPeriodRealized(frame, "all", id) ?? 0), 0) +
      (rowPeriodRealized(frame, "all", "Unclaimed") ?? 0);
    expect(sum).toBeCloseTo(frame.realized_periods.all.total, 2);
  });

  it("an UNSWEPT window is null, never 0 — for a strategy AND for Unclaimed", () => {
    // Three states. Absent is unknown; a swept window with no realization is a real zero.
    expect(rowPeriodRealized({ realized_periods: null }, "all", "MANUAL-001")).toBeNull();
    expect(rowPeriodRealized({ realized_periods: null }, "all", "Unclaimed")).toBeNull();
    expect(rowPeriodRealized(frame, "1D", "MANUAL-001")).toBe(0);
  });
});
