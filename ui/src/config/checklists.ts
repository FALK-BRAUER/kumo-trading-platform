/**
 * Checklist composition root (#181) — registers named methodology definitions. Mirrors
 * `./datasources.ts`'s role: the generic `checklist/` framework stays methodology-agnostic; this file is
 * where a specific methodology's condition set, thresholds, and tier scheme live as DATA.
 *
 * "blue_flag" — ledger-provider's 8-condition entry checklist. Definition resolved from
 * predecessor-repo's `scanner/ichimoku.py:145-155` + its `CLAUDE.md` (documented as stable, Phase 1 "done" —
 * NOT the playbook doc's bare pseudocode dict, which diverges on conditions 7/8; see the #181 plan for
 * the full resolution). Uses bar CLOSE for all price comparisons (not the live tick), matching
 * predecessor-repo's own `Close.iloc[-1]` semantics — keeps the checklist stable between renders rather than
 * flickering with every tick.
 */
import { registerChecklist } from "@/lib/framework/checklist/registry";
import type { ChecklistCondition, ChecklistContext } from "@/lib/framework/checklist/types";
import { lastLevels, shiftedCloud, chikouAbovePrice } from "@/lib/ichimoku";
import { computeAdx, isRising } from "@/lib/adx";

const ADX_PERIOD = 9; // the ledger's setting (playbook + predecessor-repo default), not the textbook 14
const ADX_MIN = 20; // "no trend" floor, predecessor-repo `scanner/ichimoku.py:150`
const CHIKOU_LOOKBACK = 26;

function lastClose(bars: ChecklistContext["dailyBars"]): number | null {
  return bars.length ? bars[bars.length - 1].close : null;
}

const BLUE_FLAG_CONDITIONS: ChecklistCondition[] = [
  {
    id: "price_above_weekly_cloud",
    label: "Weekly > cloud",
    evaluate: (ctx) => {
      const price = lastClose(ctx.weeklyBars);
      const cloud = shiftedCloud(ctx.weeklyBars, CHIKOU_LOOKBACK);
      return price == null || cloud.cloudTop == null ? null : price > cloud.cloudTop;
    },
  },
  {
    id: "tenkan_above_kijun_weekly",
    label: "Weekly TK>KJ",
    evaluate: (ctx) => {
      const levels = lastLevels(ctx.weeklyBars);
      return levels?.tenkan == null || levels.kijun == null ? null : levels.tenkan > levels.kijun;
    },
  },
  {
    id: "chikou_above_price_weekly",
    label: "Weekly Chikou",
    evaluate: (ctx) => chikouAbovePrice(ctx.weeklyBars, CHIKOU_LOOKBACK),
  },
  {
    id: "cloud_green_weekly",
    label: "Weekly cloud green",
    evaluate: (ctx) => shiftedCloud(ctx.weeklyBars, CHIKOU_LOOKBACK).green,
  },
  {
    id: "price_above_daily_cloud",
    label: "Daily > cloud",
    evaluate: (ctx) => {
      const price = lastClose(ctx.dailyBars);
      const cloud = shiftedCloud(ctx.dailyBars, CHIKOU_LOOKBACK);
      return price == null || cloud.cloudTop == null ? null : price > cloud.cloudTop;
    },
  },
  {
    id: "price_above_daily_tenkan",
    label: "Daily > Tenkan",
    evaluate: (ctx) => {
      const price = lastClose(ctx.dailyBars);
      const levels = lastLevels(ctx.dailyBars);
      return price == null || levels?.tenkan == null ? null : price > levels.tenkan;
    },
  },
  {
    id: "adx_rising_and_di_separated",
    label: "ADX+DMI",
    evaluate: (ctx) => {
      if (ctx.dailyBars.length < ADX_PERIOD + 5) return null; // not enough bars for a meaningful ADX yet
      const { adx, plusDi, minusDi } = computeAdx(ctx.dailyBars, ADX_PERIOD);
      const lastAdx = adx.at(-1) ?? null;
      const lastPlusDi = plusDi.at(-1) ?? null;
      const lastMinusDi = minusDi.at(-1) ?? null;
      if (lastAdx == null || lastPlusDi == null || lastMinusDi == null) return null;
      return isRising(adx) && lastPlusDi > lastMinusDi && lastAdx >= ADX_MIN;
    },
  },
  {
    id: "price_above_200d_ma",
    label: "Daily > MA200",
    evaluate: (ctx) => {
      const price = lastClose(ctx.dailyBars);
      const ma200 = lastLevels(ctx.dailyBars)?.ma200 ?? null;
      return price == null || ma200 == null ? null : price > ma200;
    },
  },
];

registerChecklist({
  id: "blue_flag",
  name: "Blue Flag",
  conditions: BLUE_FLAG_CONDITIONS,
  veto: (ctx) => {
    const price = lastClose(ctx.weeklyBars);
    const cloud = shiftedCloud(ctx.weeklyBars, CHIKOU_LOOKBACK);
    if (price == null || cloud.cloudTop == null || cloud.cloudBot == null) return null; // insufficient data, not a veto
    if (price < cloud.cloudBot) return "weekly below cloud";
    if (price <= cloud.cloudTop) return "weekly inside cloud";
    return null;
  },
  // Matches predecessor-repo's exact tier scheme (`scanner/ichimoku.py:158-166`), including the
  // vetoed-but-condition-8-still-true branch (weekly below cloud but price still above the 200d MA reads
  // "--", not the harsher "---") — code review, #181.
  tier: (score, total, veto, results) => {
    // Unknown is NOT the same as failing — an insufficient-history row must read as "no data", never as
    // a bearish score (code review, #181: a fully-null result previously fell through to the score<2
    // branch and rendered as bearish "--").
    const unknown = results.filter((r) => r === null).length;
    if (unknown === total) return "?";
    if (veto === "weekly below cloud") return results[7] === true ? "--" : "---";
    if (veto === "weekly inside cloud") return "=";
    if (score === total) return "+++";
    if (score >= 6) return "++";
    if (score >= 4) return "+";
    if (score >= 2) return "=";
    return "--";
  },
});
