/**
 * THE BOOK SCREEN'S OWN CELLS ANSWER THE WINDOW SELECTOR (#699).
 *
 * WHY THIS FILE EXISTS, AND IT IS NOT A DUPLICATE. #699 was reported FROM THIS TILE — the operator's
 * screenshot is the Book screen — and #699's fix was shipped to two tiles behind one shared helper.
 * Every rendered test written for it drove ManagedPortfolioTile. Review reintroduced the exact
 * reported bug HERE, `{fmtUsd(book.total)}` back in BookTile's headline, and the suite stayed
 * 812/812 GREEN.
 *
 * That is "test the seam, not the unit" verbatim: `cellHeadline` was pinned, and one of the two
 * seams that consume it was not. A helper test says nothing about whether a caller renders its
 * answer, and this repo has paid for that shape five times in one day before.
 *
 * The other seam is covered by `managed-portfolio/windowHeadline.test.ts`. Same fixture, same
 * harness, deliberately — two hand-built frames would be two derivations of one fact, free to drift.
 *
 * 1D IS THE DEFAULT PERIOD (`lib/framework/store`), so the day branch is not an edge case: it is the
 * headline path most readers see. It is exercised here with real prior closes rather than asserted
 * about.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { createElement } from "react";
import type { SourceStatus, TileProps } from "@/lib/framework/types";
import type { BookConfig } from "./definition";
import { cellNamed, renderedCells } from "@/tiles/managed-portfolio/cellParser.fixture";
import { FRAME } from "@/tiles/managed-portfolio/liveFrame.fixture";

const host = vi.hoisted(() => ({
  period: "1M",
  /** Prior closes. Non-empty is what makes the 1D day branch REACHABLE — see the header. */
  ranges: new Map<string, number>(),
  unit: "$" as "$" | "%",
  sleeves: {} as Record<string, number | null>,
  cadence: {} as Record<string, { cadence: string | null; next_rebalance: string | null }>,
}));

// `unit` IS PART OF THE STATE PRODUCTION ALWAYS HAS (#586). Omitting it left `unit` undefined, which
// is neither "$" nor "%", so every cell fell through to `—` and four tests here failed against a tile
// that was correct — the double could not represent production. Defaulted to "$" so existing tests
// read as before, and settable so the switch itself can be exercised.
vi.mock("@/lib/framework/store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useCockpitStore: (select: (s: unknown) => unknown) =>
    select({ period: host.period, unit: host.unit, openDetail: () => {}, openDetailForSymbol: () => {} }),
}));

// The sleeve is a fetched denominator; the harness supplies it directly so a lane's percentage can be
// asserted against a KNOWN allocation rather than whatever the endpoint happens to return.
vi.mock("@/lib/framework/useSleeves", () => ({ useSleeves: () => host.sleeves }));

// The cadence is fetched beside the sleeve (#888). Supplied per test so the SEAM — BookCell actually
// receiving and rendering it — is what is asserted, not the pure helper alone.
vi.mock("@/lib/framework/useLaneCadence", () => ({ useLaneCadence: () => host.cadence }));

vi.mock("@/lib/framework/instrument", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useTodayRanges: () => host.ranges,
}));

import { BookTile } from "./BookTile";

function render(period: string, ranges = new Map<string, number>(), frame: unknown = FRAME): string {
  host.period = period;
  host.ranges = ranges;
  const props: TileProps<BookConfig> = {
    instanceId: "book-1",
    config: {},
    data: {
      trades: frame,
      account: { account: null },
      external_activity: { external: [] },
      equity_curve: null,
    },
    status: { trades: "live" as SourceStatus },
    onConfigChange: () => {},
  };
  // BookTile reads `/health` for the #437 ownership list (it qualifies the held count), so it needs
  // a query client. Retries off and no refetching: this renders once, server-side, and a suspended
  // or retrying query would make the assertions below race a network layer that does not exist here.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnMount: false } } });
  return renderToString(
    createElement(QueryClientProvider, { client: qc }, createElement(BookTile, props)),
  );
}



const cell = (html: string, label: string) =>
  cellNamed(renderedCells(html, "text-base"), label);

describe("the harness can see this tile's cells at all", () => {
  // A parser that resolves nothing reports "no wrong headline" for free. Prove it reads the real
  // component before trusting anything it says about the numbers.

  it("renders per-strategy cells with labels and headlines", () => {
    const found = renderedCells(render("1M"), "text-base");
    expect(found.length).toBeGreaterThanOrEqual(4);
    expect(found.map((c) => c.label)).toContain("TECHIVOL-005");
    expect(cell(render("1M"), "TECHIVOL-005")!.headline).not.toBe("");
  });

  it("the LAST cell parses its own sub-line, not the footer beneath the grid", () => {
    // FOUND IN REVIEW WHILE LATENT, and it is the #596 row of all of them. The last cell's chunk
    // runs past the grid into BookTile's "unattributed" footer, which is itself a
    // `font-mono text-[10px] text-t3` div — so the positional sub-line anchor captured IT. At `all`
    // the Unclaimed cell parsed with sub = "unattributed $0.14 · All ?".
    //
    // Nothing read a last-cell sub at the time, so nothing was wrong — the first test that did would
    // have silently described the footer instead. Same species as the tint(0) collision, and fixed
    // in the one shared parser rather than in three copies.
    const c = cell(render("all"), "Unclaimed")!;
    expect(c.sub).toContain("standing");
    expect(c.sub).not.toContain("unattributed");
  });

  it("the standing level and the 1M realized DIFFER in this fixture", () => {
    // Without this they could agree and no assertion below could fail, whatever the cell is wired to.
    const by = (FRAME.realized_periods as Record<string, { by_strategy: Record<string, number> }>)["1M"]
      .by_strategy;
    const sub = cell(render("1M"), "TECHIVOL-005")!.sub;
    const standing = Number(
      /standing (-?)\$([\d,]+\.\d{2})/.exec(sub)!.slice(1).join("").replace(/,/g, ""),
    );
    expect(Math.abs(standing - by["TECHIVOL-005"])).toBeGreaterThan(1);
  });
});

describe("the headline follows the selector on the BOOK screen too (#699)", () => {
  it("at 1M the headline is the WINDOW's realized, not the standing level", () => {
    // THE REPORTED BUG, on the tile it was reported from. Reverting BookTile's headline to
    // `book.total` left the whole suite green before this test existed.
    expect(cell(render("1M"), "TECHIVOL-005")!.headline).toBe("-$57.47");
  });

  it("a flat strategy leads with the money it made, not with its empty book", () => {
    expect(cell(render("1M"), "MANUAL-001")!.headline).toBe("$1,509.72");
  });

  it("an UNSWEPT window leads with an em dash, never with $0.00", () => {
    const rp = JSON.parse(JSON.stringify(FRAME.realized_periods)) as Record<string, unknown>;
    delete rp["1M"];
    const c = cell(render("1M", new Map(), { ...FRAME, realized_periods: rp }), "TECHIVOL-005")!;
    expect(c.headline).toBe("—");
    expect(c.headline).not.toContain("0.00");
  });

  it("the standing level is demoted to the sub-line, not dropped", () => {
    const c = cell(render("1M"), "TECHIVOL-005")!;
    expect(c.sub).toContain("standing");
    expect(c.sub).toContain("standing -$28.20");
  });
});

describe("1D — the default period, and the one window with a true delta", () => {
  // TECHIVOL-005 holds CRM.XNYS, qty 6, last_px 256.48. A prior close of 250.00 gives
  // 6 x 6.48 = +38.88 — unlike its 1D realized (0.00) and its standing level (-28.20), so the
  // assertion can genuinely fail. Read off the fixture, not guessed: a first draft on the sibling
  // file named a symbol this strategy does not hold, so the day was never covered and the test
  // asserted nothing while passing.
  //
  // ONE NUMBER TO WATCH IF THIS FIXTURE IS EVER RE-CUT (review, 2026-08-29): the day move uses the
  // ENTRY basis instead of the prior close when `openedToday` is true. Here `opened_ts` is
  // 2026-08-28, so that branch is unreachable and recedes further with time — but if it were taken,
  // the day move would be 6 x (256.48 - 261.18) = -28.20, which is EXACTLY the standing level. The
  // test would then pass against the wrong quantity. Keep the fixture's `opened_ts` in the past.
  const PRIOR = new Map<string, number>([["CRM.XNYS", 250]]);

  it("1D leads with the DAY MOVE", () => {
    expect(cell(render("1D", PRIOR), "TECHIVOL-005")!.headline).toBe("$38.88");
  });

  it("the day fact SURVIVES at non-1D windows — it is not a 1D-only line", () => {
    // MY FIRST CUT DELETED IT. Gating the whole block on `kind === "day"` removed "day -$393.54"
    // from 1W/1M/3M/All, where it is a real fact a reader wants under a window headline — the very
    // line the #699 screenshot shows. A fact taken off the screen with nothing saying so.
    //
    // SCOPED TO THE CELL, for the same reason the duplication test is: the position table renders
    // CRM's own day move too, so a page-level assertion passes with the CELL's day line deleted.
    // Verified by mutation — the page-level form did exactly that on ManagedPortfolioTile.
    const c = cell(render("1M", PRIOR), "TECHIVOL-005")!;
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
    const c = cell(render("1M", FLAT), "TECHIVOL-005")!;
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

    const c = cell(render("1D", PRIOR, frame), "TECHIVOL-005")!;
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
    const c = cell(render("1M", PRIOR, frame), "TECHIVOL-005")!;

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
    const c = cell(render("1D", PRIOR), "TECHIVOL-005")!;
    expect(c.headline).toBe("$38.88");
    expect(c.text.match(/\$38\.88/g) ?? []).toHaveLength(1);
  });

  it("with the prior-close feed DEAD, it says which quantity it fell back to", () => {
    // #298, paid for twice: the `today_ranges` plane empties and `covered` drops to 0 for every
    // strategy at once. The cell then shows realized(1D) — a different quantity under the same
    // selector — so it must NAME the fallback rather than swapping silently.
    const c = cell(render("1D", new Map()), "TECHIVOL-005")!;
    expect(c.sub).toContain("real · 1D");
  });

  it("and it SAYS the day figure is missing, rather than looking like an ordinary cell", () => {
    // Rendered, not grepped. A source scan proves the branch EXISTS; only the render proves it
    // reaches the screen. ManagedPortfolioTile had no such branch at all until #699's review, so a
    // reader on the DEFAULT portfolio view saw a normal-looking cell throughout a feed outage.
    //
    // The fixture holds positions, so `missing > 0` and the state is genuinely "unavailable" — not
    // "this strategy holds nothing", which is benign and must stay silent.
    const html = render("1D", new Map());
    expect(html).toContain("day — no prior close");
    expect(html).toContain("text-status-watch");
  });
});

describe("the per-lane cells switch against THIS LANE'S SLEEVE (#586)", () => {
  // Operator, 2026-09-06: "still no % on strategies." The panel figures switched and the lane cells did
  // not, because I deferred the sleeve denominator as "not available in this component" without
  // checking whether it could be fetched. `/strategies` returns `target` per lane; it can.

  it("renders a lane's headline as a percentage OF ITS OWN SLEEVE, not of the account", () => {
    // The whole reason #586 chose the sleeve: the same dollar figure is a very different number
    // against a 20,000 sleeve than against a ~100,000 account, and only the first is the lane's
    // performance. A cell that silently used the account would answer a different question under an
    // identical label.
    host.unit = "%";
    host.sleeves = { "TECHIVOL-005": 20000 };
    try {
      const c = cell(render("1M"), "TECHIVOL-005");
      expect(c, "the TECHIVOL-005 cell vanished — this test can no longer see its subject").toBeTruthy();
      // -57.47 on a 20,000 sleeve is -0.3%; on a ~104,777 account it would be -0.1%. The assertion
      // names the sleeve arithmetic, so substituting the account fails rather than merely differing.
      expect(c!.headline).toBe("-0.3%");
    } finally {
      host.unit = "$";
      host.sleeves = {};
    }
  });

  it("renders `—`, never a number, when the lane has NO sleeve to divide by", () => {
    // #586 is explicit: no denominator means no percentage. A fall back to the account or to the
    // book would be a percentage of the wrong base, which is worse than none — and a sleeve of 0 is
    // reachable (every `actual` on test-alpaca read 0 on 2026-09-05 while `target` read 20,000).
    host.unit = "%";
    host.sleeves = { "TECHIVOL-005": 0 };
    try {
      const c = cell(render("1M"), "TECHIVOL-005");
      expect(c!.headline).toBe("—");
    } finally {
      host.unit = "$";
      host.sleeves = {};
    }
  });

  it("is UNCHANGED in $ mode — the switch is the only thing that moves", () => {
    const c = cell(render("1M"), "TECHIVOL-005");
    expect(c!.headline).toBe("-$57.47");
  });
});

describe("the lane cell SAYS when a lane is not daily (#888)", () => {
  // On 2026-09-11 QC345-003 read as dead for seven sessions. It is monthly. The pure helper is
  // `cadenceNote.test.ts`; THIS pins the seam — the cell actually receives the fetched cadence and
  // renders it in its sub-line — because a helper nothing calls is the state the seam rule names.
  it("a monthly lane's sub-line carries the cadence and the next rebalance date", () => {
    host.cadence = { "TECHIVOL-005": { cadence: "monthly", next_rebalance: "2026-10-01" } };
    try {
      expect(cell(render("1M"), "TECHIVOL-005")!.sub).toContain("monthly · next 2026-10-01");
    } finally {
      host.cadence = {};
    }
  });

  it("a daily lane's sub-line says nothing about cadence — daily is what the liveness count assumes", () => {
    host.cadence = { "TECHIVOL-005": { cadence: "daily", next_rebalance: null } };
    try {
      const sub = cell(render("1M"), "TECHIVOL-005")!.sub;
      expect(sub).not.toContain("daily");
      expect(sub).not.toContain("cadence");
    } finally {
      host.cadence = {};
    }
  });

  it("a lane the fetch could not describe renders 'cadence —', never silently as daily", () => {
    host.cadence = {};
    expect(cell(render("1M"), "TECHIVOL-005")!.sub).toContain("cadence —");
  });
});
