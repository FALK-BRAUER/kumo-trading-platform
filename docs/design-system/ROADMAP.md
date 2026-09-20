# Design-system implementation roadmap

Not a one-shot. Strangler migration + a few new feature surfaces. Each phase = 1+ codex-reviewed PRs,
deployed + browser-verified. Tracking epic: **#104**.

## Phases

**Phase 0 — Version the design** ✅ (this doc + `STYLE_GUIDE.md` + `mock-v2.html`).

**Phase 1 — Token layer** (foundational, small)
`tokens.ts` (single JS+CSS source) → emit `@theme` vars in `globals.css` + export raw values for chart colours.
Semantic status roles (bull/hold · bear/exit · watch · warn · info), 3 greys, **day + night**, `text-size-adjust`,
`<!DOCTYPE>` guard. No visual change yet — just the substrate.

**Phase 2 — Primitives + style-guide route**
`ui/src/components/ds/`: `TileShell` · `DataRow` (the unified row) · `MetricCell` · `StatusBadge` · `StrategyTag`
· `Sparkline` · `AccountHeader` · `SortHeader` · `ActionToggle`. A `/dev/ui` (or `styleguide` tile) rendering
every primitive + state. Encodes the row/format contract + frozen-identity column.

**Phase 3 — Migrate the list tiles (strangler)**
Orders first (reference) → Watch → Positions/Managed → Account onto the primitives. Then **ESLint** bans raw
`text-(green|red|emerald)-*` and raw `<table>` outside `DataTable`. Consistency can't regress after this.

**Phase 4 — Feature surfaces** (each its own ticket; sequence by value)
Safety (armed/disarmed + kill-switch + confirm) → Alerts/notifications → Home/portfolio-risk → semi-auto
**automation panel** (#55) → **systematic-strategy surfaces** (#193) → reconciliation UI → post-trade
analytics → journaling → session-rules.

The systematic surfaces are a **sibling** of the semi-auto panel, not a replacement. #55 manages ONE symbol
at a time on a leash; #185 decides across a whole pool once per session with no human in the loop. Both run
on the same primitives, and the two must stay visually distinguishable — a per-symbol manager and a
portfolio-level strategy are different kinds of thing and should not read alike.

## Surface → ticket mapping

### Aligns with existing tickets (design-refresh under #104, no new ticket)
| Surface in mock | Ticket(s) |
|---|---|
| Board chrome · tabs · tile mechanism · reflow | #7 · #29 · #60 |
| Detail surface + context-variants | #27 · #70 · #71 |
| Orders blotter · cancel/modify · brackets | #33 · #34 |
| Positions / Managed-Portfolio · cycle drill-down | #77 |
| Account header | #41 |
| Order ticket (strategy-first ✓ · assisted · mechanisms · actions) | #51 · #67 · #65 · #50 |
| Chart (multi-tf · Ichimoku · cloud ✓) | #58 |
| Symbol search | #25 |
| Settings (✓ exists) + session-rules extension | #49 |
| States — health · stale · quarantine | #26 · #28 · #79 |
| Cross-strategy risk · correlation · heat · sleeves | #12 · #80 |
| Semi-auto automation panel · arm-on-fill · event log · algos | #55 · #37 · #38 · #46 · #47 |
| Component registry + replacement | #7 · #50 · #65 · #55 |
| **Systematic strategy — status · book · pool · log** | **#185 · #186 · #187 · #188 · #191 · #192 · #193** |

### New — filed (features beyond the design system)
- **Design-system epic** → **#104**
- **Flexible navigation** — registered menu items → tab-sets (work centers) → **#106**
- **Home / landing surface** (P&L + portfolio risk) → **#107**
- **Order safety UX** — armed/disarmed + kill-switch + confirm-destructive → **#108**
- **Alerts / notifications** (toast + inbox) → **#109**
- **Journaling / trade-review / audit log** → **#110**
- **Broker reconciliation UI** (local vs broker + resync; extends #79) → **#111**
- **Post-trade analytics** (Sharpe / drawdown / slippage by strategy) → **#112**
- **Session / market-hours rules** (no-trade windows · overnight · auto-cancel) → **#113**
- **Systematic strategy runtime** — symbol pool with layered sources + whitelist/blacklist, lifecycle with a
  SHADOW dry-run state, decision-level action log, portfolio drift → epic **#185** (phases #186 · #187 ·
  #188 · #189 · #190 · #191 · #192, UI **#193**). Mocked in `mock-v2.html`; four patterns added to
  `STYLE_GUIDE.md`.

### Deferred (the operator's call)
AI briefs · L2 depth ladder (Alpaca stocks are L1-only; L2 = crypto).

**Un-deferred:** BCT-copy (#54) and the scanner (#45) return as **pool sources** (#191) rather than
candidate-review queues. The feed is the same; what changed is what sits downstream of it — a source
contributes a set that is replaced wholesale on refresh, instead of a queue a human approves one name at a
time. Both can share one importer.

### Shipped since the last pass (moved out of "open proposals")
- **Multi-timeframe trend (1H/1D/1W/1M/1Y) + multi-cloud (15m/1h/1d) on Watchlist** (#180) — the row/format
  contract's 2-line rule tension (write-up in `proposal-multi-trend-cloud.md`) was accepted as a real,
  the operator-approved density tradeoff: the row grows to 3 lines on the Watchlist specifically. Mocked in
  `mock-v2.html`.
- **VWAP as its own main-row column** (#182) — was sub-line KPI text; Operator: "deserves a true column".
- **Configurable KPI framework + registry** (`@/lib/framework/kpi`, `config/kpis.ts`, #182) — volume,
  today's range, prior close, market cap, beta, EPS, P/E, dividend; an unregistered/misconfigured id is
  skipped, never a crash. Same registry pattern as Layer 8's other registries (STYLE_GUIDE.md).
- **Fundamentals data plane** (#182) — market cap / beta / EPS / P/E / dividend, engine-side FMP poller
  (6h cadence), published over the existing Redis→WS pipe + the Nautilus msgbus (`customdataclass`) for
  in-process strategy consumption.
- **Gear-icon sort/filter/KPI-select popover on Watchlist** (#182 follow-up, Phase 4/5) — sort by any base
  field or registered KPI; filter by signal status (WATCH/HOLD/EXIT), Blue Flag tier, 1d cloud position
  (AND-composed); KPI multi-select. localStorage-persisted per tile instance. **Diverges from the
  `SortHeader`/`FilterChips` primitive proposal** (STYLE_GUIDE.md Layer 2, mocked in `mock-v2.html`'s
  "Table controls" section) — one popover instead of per-column click targets + a permanent filter-chip
  row; that proposal stays open for Orders/Positions.
- **Slide-to-confirm on order submit** (#108) · **slide-to-delete on Watchlist** (#64) — both shipped.
- **Systematic-strategy surfaces mocked** (#193, this pass) — status · our book · symbol pool · action log,
  in `mock-v2.html`. Four patterns went into `STYLE_GUIDE.md`: **provenance chips** (why a symbol is in the
  pool), **loud stale sources** (a stale source blocks the session, so it cannot be a quiet grey dot),
  **distance-to-exit** as the lead metric for a trailed position (not unrealised %), and
  **deliberate-but-unusual states get a label, never a colour** (`ORPHAN` is expected, not an error).
  Naming: the dry-run state is **SHADOW**, never ARMED — `managers.py` already uses ARMED for "attached and
  waiting to trigger" (#55), and two ARMEDs in one engine is a merge bug in waiting.
