/**
 * THE PER-STRATEGY HEADLINE MUST ANSWER THE SELECTED WINDOW (#699).
 *
 * 2026-08-29, from the live 1M screen: the cell's largest number is a standing LEVEL sitting
 * under a window selector, beside a hero that reads DELTA NET · 1M.
 *
 *     TECHIVOL-005
 *     $790.82                        <- headline: session realized + standing unrealized. A LEVEL.
 *     day -$393.54                   <- a 1D fact
 *     11 held · standing · real -$57.47   <- the only windowed figure, in the small print
 *
 * Three time bases in one cell, with the largest type on the one the selector does not govern.
 *
 * WHAT THIS FIXES, AND WHAT IT DELIBERATELY DOES NOT. The ticket's ideal is
 * `realized(W) + Δunrealized(W)` per lane — the same identity the account headline uses. That is not
 * buildable today: the broker publishes ACCOUNT-level equity curves only, so there is no per-lane
 * mark at a window's start and a position opened inside the window has none at all. Persisting a
 * per-lane unrealized snapshot per day is the real fix (#699 option a) and would cover windows only
 * from the day it ships.
 *
 * So the headline becomes REALIZED FOR THE WINDOW, NAMED (#699 option b). It is honest, it is
 * already computed, and it is the figure the panel's rows sum to. The cost is stated rather than
 * hidden: this headline is realized-only while DELTA NET above it is realized + Δunrealized, so the
 * two compose differently and the cell says which one it is.
 *
 * WHAT IS REFUSED, still. `realized(W) + CURRENT standing unrealized` — option (c) — reports months
 * of accrued mark as this window's performance. That is #336 exactly and it is not on the table.
 *
 * 1D KEEPS THE DAY MOVE, because 1D is the one window that IS answerable: `dayMoveByStrategy`
 * measures qty x (mark - prior close) on what is held, which is a true delta over that window.
 *
 * Driven through the REAL component. `books.test.ts` pins the row list and every one of those tests
 * passed while this tile rendered the wrong number in the largest type on the screen — because the
 * defect is in what the CELL shows, not in which rows exist.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { createElement } from "react";
import type { SourceStatus, TileProps } from "@/lib/framework/types";
import type { ManagedPortfolioConfig } from "./definition";
import { cellNamed, renderedCells } from "./cellParser.fixture";
import { FRAME } from "./liveFrame.fixture";

const host = vi.hoisted(() => ({
  period: "1M",
  /** Prior closes, so the 1D branch can actually be COVERED. Mutable for the same reason `period` is. */
  ranges: new Map<string, number>(),
  /** The `$` / `%` toggle (#392). Mutable for the same reason `period` is: the headline answers both
   *  axes of one question, and the percent case below drives the REAL component through this. */
  unit: "$" as "$" | "%",
  /** Per-lane sleeves — the denominator a lane percentage is taken against (#586). Empty means "the
   *  read failed", which must render `—` rather than a percentage of something else. */
  sleeves: {} as Record<string, number | null>,
}));

vi.mock("@/lib/framework/store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useCockpitStore: (select: (s: unknown) => unknown) =>
    select({ period: host.period, unit: host.unit, openDetail: () => {}, openDetailForSymbol: () => {} }),
}));

vi.mock("@/lib/framework/useSleeves", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useSleeves: () => host.sleeves,
}));

vi.mock("@/lib/framework/instrument", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useTodayRanges: () => host.ranges,
}));

import { ManagedPortfolioTile } from "./ManagedPortfolioTile";

function render(period: string, frame: unknown = FRAME, ranges = new Map<string, number>(), base?: unknown): string {
  host.period = period;
  host.ranges = ranges;
  const props: TileProps<ManagedPortfolioConfig> = {
    instanceId: "portfolio-1",
    config: {},
    data: {
      trades: frame,
      account: { account: null },
      managers: { managers: [] },
      external_activity: { external: [] },
    },
    status: { trades: "live" as SourceStatus },
    onConfigChange: () => {},
  };
  // THE TILE NOW READS `/pnl/unrealized-base` for the per-lane window base (#699), so it needs a query
  // client — the same reason `bookCellHeadline.test.ts` already wraps its render. Retries off and no
  // refetching: this renders once, server-side, and a retrying query would race a network layer that
  // does not exist here. The query resolves to nothing, which is the correct "no base yet" state.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnMount: false } } });
  // THE NET TERMS (#699 a), seeded when a test supplies them — the tile fetches them through
  // react-query and server-side rendering has no network.
  if (base !== undefined) qc.setQueryData(["pnl-unrealized-base"], base);
  return renderToString(
    createElement(QueryClientProvider, { client: qc }, createElement(ManagedPortfolioTile, props)),
  );
}



const cell = (html: string, label: string) =>
  cellNamed(renderedCells(html, "text-sm", "grid-cols-2 gap-px"), label);

describe("the fixture can express the defect", () => {
  // If the standing level and the window's realized happen to be equal, no assertion below can fail
  // however the cell is wired — the two sources would AGREE while the wire was cut.

  it("the standing level and the 1M realized DIFFER for TECHIVOL-005", () => {
    const by = (FRAME.realized_periods as Record<string, { by_strategy: Record<string, number> }>)["1M"]
      .by_strategy;
    expect(by["TECHIVOL-005"]).toBeCloseTo(-57.47, 2);

    // The two figures must genuinely DIFFER, or every assertion below passes whichever one the cell
    // happens to render. Read the standing level off the sub-line, which carries it after the fix —
    // reading it off the HEADLINE would make this property describe the pre-fix state and go stale
    // the moment the fix lands, which is exactly what my first draft did.
    //
    // Calibrated to the FIXTURE (one held cycle, -28.20), not to the live screen it was copied from
    // (eleven cycles, $796.52). Writing the live number here would have been a test that could only
    // pass by accident.
    const sub = cell(render("1M"), "TECHIVOL-005")!.sub;
    const standing = Number(/standing (-?)\$([\d,]+\.\d{2})/.exec(sub)!.slice(1).join("").replace(/,/g, ""));
    expect(standing).toBeCloseTo(-28.2, 2);
    expect(Math.abs(standing - by["TECHIVOL-005"])).toBeGreaterThan(1);
  });

  it("a strategy exists whose 1M window was never swept, so the unknown state is reachable", () => {
    const rp = JSON.parse(JSON.stringify(FRAME.realized_periods)) as Record<string, unknown>;
    delete rp["1M"];
    const frame = { ...FRAME, realized_periods: rp };
    expect(cell(render("1M", frame), "TECHIVOL-005")).toBeTruthy();
  });
});

describe("the headline follows the window selector (#699)", () => {
  it("at 1M the headline is the WINDOW's realized, not the standing level", () => {
    const c = cell(render("1M"), "TECHIVOL-005")!;
    expect(c.headline).toBe("-$57.47");
  });

  it("and it NAMES the window, because an unlabelled figure under a selector is the whole complaint", () => {
    const c = cell(render("1M"), "TECHIVOL-005")!;
    expect(c.sub).toContain("1M");
    expect(c.sub).toContain("real");
  });

  it("the standing level is not lost — it moves to the sub-line and says it is standing", () => {
    const c = cell(render("1M"), "TECHIVOL-005")!;
    expect(c.sub).toContain("standing");
    expect(c.sub).toContain("standing -$28.20");
  });

  it("MANUAL-001, flat, leads with the money it made rather than with a zero book", () => {
    // The lane holds nothing, so its standing level is $0.00 and its 1M realized is +1,509.72.
    // Leading with the level is how a lane that made the second-most money reads as one that made none.
    const c = cell(render("1M"), "MANUAL-001")!;
    expect(c.headline).toBe("$1,509.72");
  });

  it("an UNSWEPT window leads with an em dash, never with $0.00", () => {
    // Three states. `null` is "not swept"; rendering it as a number is the claim this repo keeps
    // paying for, and it is worse in the headline than it ever was in the sub-line.
    const rp = JSON.parse(JSON.stringify(FRAME.realized_periods)) as Record<string, unknown>;
    delete rp["1M"];
    const c = cell(render("1M", { ...FRAME, realized_periods: rp }), "TECHIVOL-005")!;
    expect(c.headline).toBe("—");
    expect(c.headline).not.toContain("0.00");
  });
});

describe("1D is the window that IS answerable, and keeps its own answer", () => {
  // TECHIVOL-005 holds CRM.XNYS in the fixture: qty 6, last_px 256.48. A prior close of 250.00 makes
  // the day move 6 x (256.48 - 250.00) = +38.88, which is nothing like its 1D realized (0.00) or its
  // standing level (-28.20) — so an assertion on the headline can genuinely fail.
  //
  // Read OFF the fixture rather than guessed: my first draft named ARKK.BATS, which TECHIVOL does not
  // hold, so the day was never covered and both tests below passed while asserting nothing.
  //
  // ONE NUMBER TO WATCH IF THIS FIXTURE IS EVER RE-CUT (review, 2026-08-29): the day move uses the
  // ENTRY basis instead of the prior close when `openedToday` is true. Here `opened_ts` is
  // 2026-08-28, so that branch is unreachable and recedes further with time — but if it were taken,
  // the day move would be 6 x (256.48 - 261.18) = -28.20, which is EXACTLY the standing level. The
  // test would then pass against the wrong quantity. Keep the fixture's `opened_ts` in the past.
  const PRIOR = new Map<string, number>([["CRM.XNYS", 250]]);

  it("the fixture can COVER a day move at all", () => {
    // The first version of this test ran with an EMPTY prior-close map, so the day was never covered,
    // the cell always fell through to the window branch, and the test asserted only the sub-line —
    // it passed no matter what the headline did, while its name claimed to pin the headline.
    const c = cell(render("1D", FRAME, PRIOR), "TECHIVOL-005")!;
    expect(c.sub).toContain("day");
    expect(c.sub).not.toContain("real · 1D");
  });

  it("1D leads with the DAY MOVE, not with realized", () => {
    // `dayMoveByStrategy` is qty x (mark - prior close) on what is held — a TRUE delta over the
    // window, on the same basis rule Home uses. Replacing it with realized would throw away the one
    // window that can be answered properly.
    const c = cell(render("1D", FRAME, PRIOR), "TECHIVOL-005")!;
    expect(c.headline).toBe("$38.88");
  });

  it("the day fact SURVIVES at non-1D windows — it is not a 1D-only line", () => {
    // MY FIRST CUT DELETED IT. Gating the whole block on `kind === "day"` removed "day -$393.54"
    // from 1W/1M/3M/All, where it is a real fact a reader wants under a window headline — the very
    // line the #699 screenshot shows. A fact taken off the screen with nothing saying so.
    //
    // SCOPED TO THE CELL, for the same reason the duplication test is: the position table renders
    // CRM's own day move too, so a page-level assertion passes with the CELL's day line deleted.
    // Verified by mutation — the page-level form did exactly that on ManagedPortfolioTile.
    const c = cell(render("1M", FRAME, PRIOR), "TECHIVOL-005")!;
    // "day $38.88" as ONE string. Asserting the bare word "day" would be ballast — the sub-line's own
    // "· day —" satisfies it at 1D — and asserting the number alone would be satisfied by the
    // headline. The pair is what only the day LINE can produce.
    expect(c.text).toContain("day $38.88");
  });

  it("a day move of exactly ZERO does not let the day line impersonate the sub-line", () => {
    // THE tint(0) COLLISION, found in review while still latent. `tint(0)` returns `text-t3`, which
    // is byte-identical to the sub-line's class, and the day line comes FIRST in the DOM — so a
    // class-anchored parser taking the first match would read "day $0.00" as the sub-line and every
    // sub assertion would silently describe the wrong element.
    //
    // Not reachable with the base fixture (its day move is 38.88) and entirely reachable with a flat
    // one, which is an ordinary thing to want to test. The shared parser anchors the sub-line
    // POSITIONALLY (last text-[10px] block in the cell), which cannot collide.
    const FLAT = new Map<string, number>([["CRM.XNYS", 256.48]]);   // prior close == last_px
    const c = cell(render("1M", FRAME, FLAT), "TECHIVOL-005")!;
    expect(c.text).toContain("day $0.00");
    // The sub-line, not the day line: it names the window and carries the standing level.
    expect(c.sub).toContain("standing");
    expect(c.sub).not.toContain("day $0.00");
  });

  it("PARTIAL coverage renders the day move AND marks it understated", () => {
    // THE "+" PATH, unreachable in the base fixture because every strategy holds exactly one symbol,
    // so `covered > 0 && missing > 0` cannot occur — review's coverage gap, and it is the state
    // where the number shown is TRUE but INCOMPLETE, which is the one most worth marking.
    //
    // Built by giving TECHIVOL a second holding and a prior close for only ONE of the two. The
    // fixture property is asserted first: two holdings, one priced.
    const extra = {
      ...(FRAME.trades as Record<string, unknown>[])[3],
      instrument_id: "AMAT.XNAS",
      cycle_id: "techivol-second-holding",
    };
    const frame = { ...FRAME, trades: [...(FRAME.trades as unknown[]), extra] };
    const held = (frame.trades as Record<string, unknown>[]).filter(
      (t) => t.strategy_id === "TECHIVOL-005",
    );
    expect(held).toHaveLength(2);

    const c = cell(render("1D", frame, PRIOR), "TECHIVOL-005")!;
    // A day FIGURE, not the bare word: the sub-line's "· day —" contains "day " on its own, so that
    // clause could never fail independently (review's catch).
    expect(c.text).toMatch(/day -?\$[\d,]+\.\d{2}/);
    // The "+" is the whole point: the figure is real but understated by the unpriced leg.
    expect(c.text).toContain("+");
    expect(c.text).not.toContain("no prior close");
  });

  it("the unpriced count stays on the DAY line and out of the window descriptor", () => {
    // A DAY-plane fact must not sit beside the WINDOW descriptor. ManagedPortfolioTile briefly
    // rendered "real · 1M · 1 unpriced", which reads as "the 1M realized is missing prices" — it is
    // not; the window's realized is complete and only today's move is short a leg.
    //
    // It arrived by accident: repointing the count at the shared reading also dropped its `showDay`
    // gate, so a 1D-only fragment began rendering at every window. And only THIS tile had it, so the
    // two tiles disagreed in exactly the state the shared helper exists to unify.
    //
    // The "+" on the day line carries the same fact, with its tooltip, at every window and on both
    // tiles. One derivation, one render.
    const extra = {
      ...(FRAME.trades as Record<string, unknown>[])[3],
      instrument_id: "AMAT.XNAS",
      cycle_id: "techivol-second-holding",
    };
    const frame = { ...FRAME, trades: [...(FRAME.trades as unknown[]), extra] };
    const c = cell(render("1M", frame, PRIOR), "TECHIVOL-005")!;

    // Fixture property first: this really IS the partial state, or the assertion below is empty.
    expect(c.text).toContain("+");
    expect(c.sub).not.toContain("unpriced");
  });

  it("and it is NOT repeated when the headline already IS the day move", () => {
    // At 1D-covered the headline is $38.88 and the small line said "day $38.88" — the duplication
    // just removed from the sub-line, reintroduced one line up. It reappears only when it adds the
    // "+" that marks a figure understated by positions with no prior close.
    //
    // SCOPED TO THE CELL. Counting across the whole page fails for the wrong reason: the position
    // table below legitimately shows CRM's own day move, which equals the strategy's when the
    // strategy holds one position. That is not the duplication being pinned.
    const c = cell(render("1D", FRAME, PRIOR), "TECHIVOL-005")!;
    expect(c.headline).toBe("$38.88");
    expect(c.text.match(/\$38\.88/g) ?? []).toHaveLength(1);
  });

  it("with the prior-close feed DEAD, 1D says which quantity it fell back to", () => {
    // #298, which this repo has paid for twice: the `today_ranges` plane goes empty and `covered`
    // drops to 0 for every strategy at once. The cell then shows realized(1D) — a different
    // quantity under the same selector — so it must NAME it rather than swapping silently.
    const c = cell(render("1D", FRAME, new Map()), "TECHIVOL-005")!;
    expect(c.sub).toContain("real · 1D");
    // The separator became "· day —" in the sub-line rewrite, so the old "· day ·" form
    // could no longer appear under ANY wiring — a negative assertion that had stopped
    // being able to fail. Matched against what the day case actually renders now.
    expect(c.sub).not.toContain("· day —");
  });

  it("and it SAYS the day figure is missing, rather than looking like an ordinary cell", () => {
    // Rendered, not grepped. A source scan proves the branch EXISTS; only the render proves it
    // reaches the screen. ManagedPortfolioTile had no such branch at all until #699's review, so a
    // reader on the DEFAULT portfolio view saw a normal-looking cell throughout a feed outage.
    //
    // The fixture holds positions, so `missing > 0` and the state is genuinely "unavailable" — not
    // "this strategy holds nothing", which is benign and must stay silent.
    const html = render("1D", FRAME, new Map());
    expect(html).toContain("day — no prior close");
    expect(html).toContain("text-status-watch");
  });
});


describe("option (a): with the net terms served, the headline is the window's NET OF FLOWS (#699)", () => {
  // TECHIVOL-005 in the frame: Σ signed market_value of its deployed cycles is `mvNow`. Serve a base
  // and an invested such that the net is a number no other path in the cell could produce.
  const mvNow = (FRAME as { trades: Array<{ strategy_id: string; is_capital_deployed: boolean; market_value: number }> })
    .trades.filter((t) => t.strategy_id === "TECHIVOL-005" && t.is_capital_deployed)
    .reduce((s, t) => s + t.market_value, 0);
  const served = (partial: string | null) => ({
    by_period: { "1M": { "TECHIVOL-005": -100 } },
    market_value: { "1M": { "TECHIVOL-005": mvNow - 1234.56 } },
    net: { "1M": { "TECHIVOL-005": { mv_base: mvNow - 1234.56, invested: 234.56, partial } } },
    base_date: { "1M": "2026-07-31" }, unreadable: [], error: null,
  });

  it("the fixture's live market value is a number, so a net is computable at all", () => {
    expect(Number.isFinite(mvNow) && mvNow !== 0).toBe(true);
  });

  it("leads with mv_now − mv_base − invested and names it `net · 1M`", () => {
    const c = cell(render("1M", FRAME, new Map(), served(null)), "TECHIVOL-005")!;
    expect(c.headline).toBe("$1,000.00");
    expect(c.sub).toContain("net · 1M");
    expect(c.sub).toContain("real");      // FIFO realized stays in the small print
  });

  it("a PARTIAL window is marked and its reason is on the mark", () => {
    const html = render("1M", FRAME, new Map(), served("not captured: 2026-09-09, 2026-09-10"));
    expect(html).toContain("PARTIAL — not captured: 2026-09-09, 2026-09-10");
  });

  it("BOTH notes reach the mark when a window is partial for two reasons — a truncating renderer would drop the gap", () => {
    const html = render("1M", FRAME, new Map(), served("first observed 2026-09-11; not captured: 2026-09-10; internal fills: repair 2026-09-13 (1)"));
    expect(html).toContain("first observed 2026-09-11; not captured: 2026-09-10; internal fills: repair 2026-09-13 (1)");
  });

  it("with the terms UNKNOWN the cell shows what it showed before — window realized, not zero", () => {
    const c = cell(render("1M", FRAME, new Map(), {
      by_period: { "1M": { "TECHIVOL-005": -100 } }, market_value: { "1M": { "TECHIVOL-005": null } },
      net: { "1M": { "TECHIVOL-005": { mv_base: null, invested: null, partial: null } } },
      base_date: { "1M": "2026-07-31" }, unreadable: [], error: null,
    }), "TECHIVOL-005")!;
    expect(c.headline).toBe("-$57.47");
  });

  it("1D is untouched by served net terms", () => {
    const before = render("1D", FRAME, new Map());
    const after = render("1D", FRAME, new Map(), served(null));
    expect(after).toEqual(before);
  });
});

/**
 * THE SAME LANE, THE SAME NUMBER, ON BOTH TABS (#392).
 *
 * 2026-09-14 06:45Z, from the phone: *"ibkr-paper — 2 tabs disagree on momentum p&l"*. Home read
 * `MOMENTUM-002 -0.0%` and Portfolio read `MOMENTUM-002 -$12.87` with `%` selected. One value, two
 * units: this cell rendered `fmtUsd` unconditionally and never read the toggle.
 *
 * `laneCellUnitSwitch.test.ts` guards the WIRING across every lane cell by reading the source. This
 * drives the REAL component and asserts the RENDER, because a file-scan cannot tell a correct
 * denominator from a plausible one — and the denominator is the whole decision here (#586: the card
 * is about the LANE, so its percentage is of the lane's sleeve, never of the account).
 */
describe("the lane cell renders in the selected unit (#392)", () => {
  const mvNow = (FRAME as { trades: Array<{ strategy_id: string; is_capital_deployed: boolean; market_value: number }> })
    .trades.filter((t) => t.strategy_id === "TECHIVOL-005" && t.is_capital_deployed)
    .reduce((s, t) => s + t.market_value, 0);
  /** The same terms the `$1,000.00` case above serves, so the two units describe ONE number. */
  const served = {
    by_period: { "1M": { "TECHIVOL-005": -100 } },
    market_value: { "1M": { "TECHIVOL-005": mvNow - 1234.56 } },
    net: { "1M": { "TECHIVOL-005": { mv_base: mvNow - 1234.56, invested: 234.56, partial: null } } },
    base_date: { "1M": "2026-07-31" }, unreadable: [], error: null,
  };

  afterEach(() => {
    host.unit = "$";
    host.sleeves = {};
  });

  it("in `%` renders the headline as a share of THIS LANE'S sleeve", () => {
    host.unit = "%";
    host.sleeves = { "TECHIVOL-005": 20_000 };
    const c = cell(render("1M", FRAME, new Map(), served), "TECHIVOL-005")!;
    // $1,000.00 of a 20,000 sleeve. The dollar case two describes up asserts the same terms render
    // "$1,000.00" — same number, same cell, the unit is the only difference.
    expect(c.headline).toBe("+5.0%");
  });

  it("takes the LANE's sleeve, not the account — the two answer different questions", () => {
    // The identical figure against a 100,000 sleeve is 1.0%, against 20,000 it is 5.0%. If this cell
    // ever reached for the account equity instead, this is the test that would say so.
    host.unit = "%";
    host.sleeves = { "TECHIVOL-005": 100_000 };
    const c = cell(render("1M", FRAME, new Map(), served), "TECHIVOL-005")!;
    expect(c.headline).toBe("+1.0%");
  });

  it("with no sleeve renders `—`, never a percentage of some other denominator", () => {
    // `useSleeves` returns an empty map when its read fails. A percentage of the wrong base is worse
    // than no percentage (#586), and silently falling back to the account is exactly that.
    host.unit = "%";
    host.sleeves = {};
    const c = cell(render("1M", FRAME, new Map(), served), "TECHIVOL-005")!;
    expect(c.headline).toBe("—");
  });

  it("with a sleeve of ZERO renders `—`, not a division by zero", () => {
    // A different failure from a MISSING sleeve and reachable on its own: a lane configured with no
    // allocation is served `target: 0`, and `percentOf` refuses `<= 0` rather than returning
    // `Infinity%` (peer review, S4). `useSleeves` also maps a non-numeric target to `null`, which the
    // case above covers; this one covers the number that is real and still unusable.
    host.unit = "%";
    host.sleeves = { "TECHIVOL-005": 0 };
    const c = cell(render("1M", FRAME, new Map(), served), "TECHIVOL-005")!;
    expect(c.headline).toBe("—");
  });

  it("leaves the carried context in dollars — `standing` does not answer the selector", () => {
    // Parity with BookTile, which switches the headline and the window realized and nothing else.
    // "% of what" has no non-circular answer for a level.
    host.unit = "%";
    host.sleeves = { "TECHIVOL-005": 20_000 };
    const c = cell(render("1M", FRAME, new Map(), served), "TECHIVOL-005")!;
    // The sign lives OUTSIDE the `$`, so a negative standing reads `-$28.20`.
    expect(c.sub).toMatch(/standing -?\$/);
    // And in the same sub-line, the window's REALIZED did switch — it is a return on the lane.
    // Both facts from one render: what switches and what does not, side by side.
    expect(c.sub).toMatch(/real -?\d+\.\d%/);
  });

  it("in `$` the same terms render the dollar figure — the toggle is the ONLY difference", () => {
    host.unit = "$";
    host.sleeves = { "TECHIVOL-005": 20_000 };
    const c = cell(render("1M", FRAME, new Map(), served), "TECHIVOL-005")!;
    expect(c.headline).toBe("$1,000.00");
  });
});
