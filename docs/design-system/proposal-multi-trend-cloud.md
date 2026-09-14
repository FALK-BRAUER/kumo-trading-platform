# Proposal: multi-timeframe trend + multi-cloud on the Watchlist row

**Status: shipped (#180).** The 3-line-row tradeoff below was accepted; kept for the record of why.

Requested by the operator 2026-07-30, off a phone screenshot of the current Watch tab. Ask: 5 sparklines
(1H/1D/1W/1M/1Y) each with its own %, "below the KPI numbers", plus Ichimoku cloud position at 15m/1h/1d
simultaneously. Mock built at `mock-v2.html` (new "Watchlist proposal" tile, right after the current
Watchlist tile) — same 3 rows (JNJ/DLLL/SMH) shown both ways for comparison.

## The real tension

`STYLE_GUIDE.md` (Layer 4, the row/format contract) is explicit and deliberate: **every list row is
exactly two lines** — one main KPI line, one always-visible sub-line (never an accordion/expander). This
isn't an accident of the current build; it's a stated scan-ability rule.

The mock I built adds a **third line** to satisfy "below the KPI numbers" literally. That's a genuine
violation of an explicit rule, not just a density nit — worth a conscious decision, not a silent
exception.

## Data feasibility — good news, no backend blocker

Checked `engine_node.py`/`feed_config.py`/`feed.toml`: the engine already loads history for **every**
granularity (1m/5m/15m/30m/1h/1d/1w) for every loaded instrument (universe + positions + watchlist),
regardless of the realtime-symbol-budget fix shipped today (2026-07-30) — that budget only gates the
LIVE tick-by-tick follow-on subscribe (trades/quotes/native 1m+1d bars), not the historical `request_bars`
call, which is unconditional. So:

- **1H** → slice of 1m bars (7-day lookback, plenty)
- **1D** → slice of 5m or 1m bars (same day)
- **1W** → slice of 1h bars (45-day lookback)
- **1M** → slice of 1d bars (200-day lookback)
- **1Y** → slice of 1d or 1w bars (200-day / ~214-week lookback)
- **15m/1h/1d clouds** → `lastLevels()` (already exists, `ichimoku.ts`) run 3× per row against 3 bar series

Every watchlist symbol gets correct historical trend/cloud data, live-tick-refreshed only if it's within
the 7-symbol realtime budget — same caveat already flagged and accepted for the existing single Trend
column. **No new backend work.**

Client-side cost: each row goes from 1 `useSource("bars", …)` call to ~5 (1m-or-5m, 15m, 1h, 1d, 1w —
15m/1h/1d shared between trend and cloud). ×17 watchlist rows ≈ 85 concurrent bar-topic subscriptions on
the shared WS plane (`useSource`/`ws-manager`) — same transport, no new Alpaca-channel cost (that's a
server-side concern already solved today), just more topic fan-out client-side. Not a scaling risk at
current watchlist size; worth a sanity check if the watchlist grows to 50+.

## Three ways to actually ship it (pick one)

**A — Break the 2-line rule for Watchlist specifically (the mock as built).**
Row grows to 3 lines: main KPI, new trend-strip (5× tiny sparkline + %), then the existing Ichimoku
sub-line with 15m/1h/1d chips folded in (`15m▲ 1h▲ 1d▲`). Fully what was asked, but it's a real,
conscious exception to an explicit design rule — every other list tile (Orders, Positions) stays 2-line,
so Watchlist becomes visually inconsistent with its siblings. Cheapest to build (mock done, wire real
data next). Tightest fit on a narrow phone — the mock's 5 sparklines are 30px wide each; readable but
dense.

**B — Keep 2 lines; make the single Trend column timeframe-switchable.**
No new line. The existing Trend sparkline gets a tiny inline period toggle (1H/1D/1W/1M/1Y — like the
mock's existing `.seg` segmented-control pattern, already used elsewhere in this file for
Last/D-P&L/U-P&L/Mkt Val). One trend line visible at a time, switch applies to the WHOLE list (one
global toggle above the table, not per-row — per-row toggles would be a tap-trap on mobile). Loses
"see all 5 at once"; keeps the row contract intact. Multi-cloud folds into the existing sub-line as chips
either way (sub-line already carries free-form text, room for `15m▲ 1h▲ 1d▲` alongside TK/KJ/Cld — or
replacing them, since TK/KJ/Cld numbers may be more than needed once the chip summary exists).

**C — Multi-timeframe trend + multi-cloud lives on the DETAIL surface, not the list row.**
Ticket #58 already scopes "Chart (multi-tf · Ichimoku · cloud)" as a chart-surface feature. The list
row keeps its current single-glance sparkline (the row's whole job is fast scanning across many
symbols); tapping a symbol opens the detail view, which has actual room for 5 real (not 30px) trend
charts + a proper 3-granularity cloud readout. This is the option that costs the list row nothing and
reuses a surface that's designed for depth. **Recommended** — it resolves the tension by not putting a
scanning surface and a depth surface in visual competition.

## My recommendation

C for the real feature (multi-timeframe depth belongs on the tap-in detail surface, which #58 already
tracks), with B as a light Watchlist-row enhancement if a global period toggle is wanted for the existing
single Trend column. A is available if you want the everything-on-one-row density regardless of the
contract break — the mock exists and is ready to wire if so.

## Effort (rough, once a direction is picked)

- **Backend:** ~0 — data already flows for every granularity.
- **A (list-row, 3 lines):** new `TrendStrip` primitive (5× mini-sparkline + %), extend the Ichimoku
  sub-line renderer for cloud chips, wire 5 `useSource` calls per row (or one batching hook to cut
  request count). Medium — a few days including the STYLE_GUIDE contract discussion/sign-off.
- **B (2-line, switchable):** one segmented control + reuse of the existing single-Trend rendering path
  with a period prop, cloud chips in the sub-line. Small — under a day once the toggle UX is settled.
- **C (detail surface):** extends whatever #58's chart tile build already needs; this ask just adds
  "show cloud badges for 15m/1h/1d simultaneously" to that ticket's scope, not new plumbing.

## Open question for the operator

Is breaking the list row's 2-line contract (option A) something you actually want for Watchlist, or was
"below the KPI numbers" describing intent (more detail, easy to find) rather than a hard placement
requirement? If the latter, C gets you the full depth with zero list-row cost.
