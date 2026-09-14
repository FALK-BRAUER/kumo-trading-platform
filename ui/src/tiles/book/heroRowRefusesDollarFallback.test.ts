/**
 * THE HERO ROW REFUSES TO PRINT DOLLARS UNDER A `%` TOGGLE (#1077).
 *
 * Found by the kumo-strategies session cross-reviewing #1076 (S2). Two renderers of the unit toggle sit
 * in `BookTile.tsx`. The lane cell's `laneFigure` refuses when no honest percentage exists:
 *
 *     return pct === null ? "—" : fmtPct(pct);
 *
 * The hero row's `inUnit`, same file, fell back to dollars:
 *
 *     return pct === null ? fmtUsd(value) : fmtPct(pct);
 *
 * Both cited #586. Together they meant the Book panel could print `-$57.47` while the toggle said `%`
 * — one figure in the unit the reader did NOT select, beside others in the unit they did, with nothing
 * saying which was which. That is #392's own complaint class one panel up from where it was just fixed:
 * "it should not add %. it should switch all relative change fields to %" — a field that silently
 * declines to switch is the same failure as a field that never could.
 *
 * REACHABLE, NOT HYPOTHETICAL. `inUnit` fell back whenever `percentOf` returned null, which is any
 * missing, zero or negative denominator: `curves[period].base_value` absent for a window (`baseValue`
 * already documents `0.0` arriving for an absent base), a node with no broker account, `deployed` at
 * zero on a flat book. This harness reproduces the first: a swept frame and NO equity curve, so
 * REALIZED has a value and its denominator does not.
 *
 * Driven through the REAL tile, the same way `bookCellHeadline.test.ts` drives it, because the defect
 * is in what the panel PRINTS — a unit test of `inUnit` would have to reimplement the call.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { createElement } from "react";
import type { SourceStatus, TileProps } from "@/lib/framework/types";
import type { BookConfig } from "./definition";
import { FRAME } from "@/tiles/managed-portfolio/liveFrame.fixture";

const host = vi.hoisted(() => ({
  period: "1M",
  unit: "$" as "$" | "%",
}));

vi.mock("@/lib/framework/store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useCockpitStore: (select: (s: unknown) => unknown) =>
    select({ period: host.period, unit: host.unit, openDetail: () => {}, openDetailForSymbol: () => {} }),
}));
vi.mock("@/lib/framework/useSleeves", () => ({ useSleeves: () => ({}) }));
vi.mock("@/lib/framework/useLaneCadence", () => ({ useLaneCadence: () => ({}) }));
vi.mock("@/lib/framework/instrument", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useTodayRanges: () => new Map<string, number>(),
}));

import { BookTile } from "./BookTile";

/**
 * A curve with a P&L but NO base — `base_value: 0`, which is how `exec_client` serialises an absent
 * base (`float(raw.get("base_value") or 0.0)`; `baseValue` documents it) — and no account. So NET
 * has a value (the broker's to-last-close `pnl` stands in), REALIZED has a value (the frame is
 * swept), Δ UNREALIZED has a value (NET − REALIZED), and the denominator for all three is unknown.
 * Every window-based figure on the hero row is therefore reachable with something to fall back to.
 */
const CURVE_WITHOUT_BASE = { curves: { "1M": { pnl: 500, base_value: 0, covered: true, covers_days: 30 } } };

/**
 * FRAME with every mark stripped: `deployedValue` then skips every held position and counts it in
 * `unmarked`, so `deployed` is 0 WHILE positions are held — the one reachable state in which
 * SECURED's denominator is gone (peer review, S2). The venue withholding market data is not a
 * hypothetical; the tile's own SECURED tooltip already describes it.
 */
type Trade = Record<string, unknown> & {
  is_capital_deployed?: boolean; side?: string; avg_px_open?: number;
  working_orders?: Array<Record<string, unknown>>;
};
/**
 * AND one resting stop lifted ABOVE its entry, so `secured.value` is > 0 — `securedValue` is
 * Σ (stop − entry) × qty and never reads a mark, so a positive secured figure with no live prices is
 * a reachable book, not a contrivance. Without it the SECURED colour gate is dead in this harness:
 * the first cut's "not painted bull" assertion passed with the gate deleted, because secured was $0.00
 * and the cell was grey either way.
 */
const UNMARKED_FRAME = (() => {
  const trades = (FRAME as { trades: Trade[] }).trades.map((t) => {
    const { market_value: _mv, last_px: _px, ...rest } = t;
    return rest as Trade;
  });
  const first = trades.find((t) => t.is_capital_deployed && t.side !== "SHORT" && typeof t.avg_px_open === "number" && (t.working_orders?.length ?? 0) > 0)!;
  first.working_orders![0] = { ...first.working_orders![0], trigger_price: (first.avg_px_open as number) + 10 };
  return { ...(FRAME as object), trades };
})();

/**
 * The broker's own swept figure for the window, DIFFERENT from the legs' realized, so the `broker …`
 * comparison label and the tooltip sentence both have something to say — and can be seen withheld.
 */
const WITH_BROKER = { ...(FRAME as object), realized_periods_swept: { "1M": { net: 4200 } } };

function render(unit: "$" | "%", frame: unknown = WITH_BROKER): string {
  host.unit = unit;
  const props: TileProps<BookConfig> = {
    instanceId: "book-1",
    config: {},
    data: {
      trades: frame,
      account: { account: null },
      external_activity: { external: [] },
      equity_curve: CURVE_WITHOUT_BASE,
    },
    status: { trades: "live" as SourceStatus },
    onConfigChange: () => {},
  };
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnMount: false } } });
  return renderToString(createElement(QueryClientProvider, { client: qc }, createElement(BookTile, props)));
}

/** The text of the hero `Metric` whose label starts with `text`, or null when it is not on screen. */
function metric(html: string, label: string): { value: string; cls: string; title: string | null; sub: string | null } | null {
  // A Metric is `<div title?><div class=label>LABEL</div><div class="font-mono text-sm CLS">VALUE…</div><div class=sub>SUB</div>?`.
  const re = new RegExp(
    `<div class="bg-surf px-3 py-2"(?: title="([^"]*)")?><div class="text-\\[10px] uppercase tracking-wider text-t3">${label}[^<]*</div><div class="font-mono text-sm([^"]*)">([^<]*)(?:<[^>]*>[^<]*</span>)?</div>(?:<div class="font-mono text-\\[10px] text-t3">([^<]*)</div>)?`,
  );
  const m = html.match(re);
  return m ? { value: m[3], cls: m[2].trim(), title: m[1] ?? null, sub: m[4] ?? null } : null;
}

/** The NET hero's value and its VISIBLE sub-line — different markup from a Metric. */
function netHero(html: string): { value: string; sub: string } | null {
  const m = html.match(/font-mono text-2xl font-semibold[^"]*"[^>]*>([^<]*)<\/div><div class="text-\[10px] text-t3">([^<]*)</);
  return m ? { value: m[1], sub: m[2] } : null;
}

describe("the hero row under a % toggle with no window base (#1077)", () => {
  it("the harness reaches the case: NET, REALIZED and Δ UNREALIZED all have a dollar value to fall back to", () => {
    // If any of these reads `—` in `$` the fallback is unreachable for it from here and the test
    // below asserts nothing about it. Seen in the first cut: with `equity_curve: null` NET and Δ were
    // null and their assertion passed on the pre-fix tree — a green that meant nothing.
    const html = render("$");
    for (const label of ["Realized", "Δ Unrealized"]) {
      const m = metric(html, label);
      expect(m, label).not.toBeNull();
      expect(m!.value, label).toMatch(/^-?\$[\d,]+\.\d\d/);
    }
    const net = html.match(/font-mono text-2xl font-semibold[^"]*"[^>]*>([^<]*)</);
    expect(net![1]).toMatch(/^-?\$[\d,]+\.\d\d/);
  });

  it("REALIZED refuses with `—` rather than printing a dollar figure the reader did not ask for", () => {
    const m = metric(render("%"), "Realized");
    expect(m).not.toBeNull();
    expect(m!.value, "a dollar figure under a % toggle").not.toMatch(/\$/);
    expect(m!.value).toMatch(/^—/);
  });

  it("and SAYS WHY on the title — the refusal names the missing denominator", () => {
    const m = metric(render("%"), "Realized");
    expect(m!.title ?? "").toMatch(/^no 1M base to take % against — shown as —/);
  });

  it("the same refusal reaches every window-based figure, not just the one this was found on", () => {
    const html = render("%");
    const m = metric(html, "Δ Unrealized");
    expect(m).not.toBeNull();
    expect(m!.value, "Δ Unrealized prints dollars under %").not.toMatch(/\$/);
    // NET is the hero above the Metrics and has its own markup; it must not print dollars either.
    const net = netHero(html);
    expect(net).not.toBeNull();
    expect(net!.value).not.toMatch(/\$/);
  });

  it("puts the reason ON THE SCREEN, not only on a hover title — #392 was reported from a phone (S1)", () => {
    // A `title` never renders on mobile. Pre-fix the phone showed a wrong-unit number; a title-only
    // refusal would have it show an unexplained blank over a sub-line still promising "realized +
    // change in unrealized … per the broker". The visible sub carries the short reason.
    const html = render("%");
    expect(netHero(html)!.sub).toMatch(/no 1M base to take % against — switch to \$/);
    expect(metric(html, "Realized")!.sub ?? "").toMatch(/^no 1M base to take % against/);
    expect(metric(html, "Δ Unrealized")!.sub ?? "").toMatch(/^no 1M base to take % against/);
  });

  it("in `$` nothing changes — the refusal is a `%`-only behaviour", () => {
    const m = metric(render("$"), "Realized")!;
    expect(m.value).toMatch(/\$/);
    expect(m.title ?? "").not.toMatch(/to take % against/);
    expect(m.sub ?? "").not.toMatch(/to take % against/);
  });

  it("the broker comparison is withheld in the same state, in the sub AND in the title (point 4)", () => {
    // `broker —` beside a realized `—` names a disagreement nobody can read, and "the broker's own
    // ledger says — net" inside the tooltip is the same sentence with more words. One rule, both
    // places — the reviewer's bite on #1076's first cut found the label gated and the sentence not.
    // REACHABLE FIRST: in `$` both render, so their absence in `%` is a withholding, not a fixture
    // that never had them.
    const d = metric(render("$"), "Realized")!;
    expect(d.sub ?? "").toMatch(/broker \$/);
    expect(d.title ?? "").toMatch(/ledger says \$/);
    const m = metric(render("%"), "Realized")!;
    expect(m.sub ?? "").not.toMatch(/broker/);
    expect(m.title ?? "").not.toMatch(/ledger says/);
  });
});

describe("SECURED under a % toggle when nothing held has a mark (#1077, S2)", () => {
  it("the harness reaches the case: positions are held, deployed is $0.00 with an unmarked count", () => {
    const html = render("$", UNMARKED_FRAME);
    const d = metric(html, "Deployed");
    expect(d).not.toBeNull();
    expect(d!.value).toBe("$0.00");
    expect(html).toMatch(/without a mark/);
  });

  it("refuses with `—`, and the dash is not painted bull", () => {
    // Reachable first: in `$` the same book shows a positive, bull-coloured secured figure.
    const d = metric(render("$", UNMARKED_FRAME), "Secured")!;
    expect(d.value).toMatch(/^\$[1-9]/);
    expect(d.cls).toMatch(/status-bull/);
    const m = metric(render("%", UNMARKED_FRAME), "Secured")!;
    expect(m.value).toMatch(/^—/);
    expect(m.cls).not.toMatch(/status-bull/);
  });

  it("says what is actually missing — the marks, not the capital (point 2)", () => {
    // "Nothing deployed" beside a Deployed cell reading `$0.00+ · N without a mark` would be two
    // cells contradicting each other about the same book.
    const m = metric(render("%", UNMARKED_FRAME), "Secured")!;
    expect(m.sub ?? "").toMatch(/^deployed value unknown — \d+ without a mark/);
    expect(m.title ?? "").toMatch(/deployed value unknown/);
    expect(m.title ?? "").not.toMatch(/nothing deployed/);
  });
});

