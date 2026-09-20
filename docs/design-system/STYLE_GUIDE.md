## Why — this is the substrate for the configurable tile/tab framework
The cockpit is **config-placed tiles across named tabs** (config-over-editor: layouts live in `layouts.ts`, no runtime drag-drop editor). That promise — *compose the cockpit from config* — only holds if every tile is built from a **shared token + primitive layer**. Today tiles are hand-rolled with raw Tailwind (`text-emerald-400`/`text-red-400` ad-hoc; one tile a card, another a table) → visual drift, inconsistent density, no day/night, chart colours out of sync with the DOM. **No semantic tokens, no shared primitives.** This epic makes config-placed tiles render consistently anywhere, and makes new tiles cheap. It underpins #7 (tile framework), #29 (uniform tile data resolution), #60 (responsive board), #70/#71 (context-aware detail).

Stack: **Tailwind v4** (`@theme` + shadcn OKLCH vars already in `globals.css`). Build *on* Tailwind. Reference mock (Perplexity×Codex synthesis + IBKR/moomoo inspiration): session scratchpad `ds2.html`.

## Layer 1 — Semantic tokens
- Status roles → our vocab: `bull/hold`, `bear/exit`, `watch`, `warn`, `info` (never raw palette classes again).
- Exactly **3 text greys** (primary/secondary/muted); no ad-hoc opacities.
- **Day + night**: same roles, per-mode values (green/red need deeper hexes on white than near-black). Default night; respect `prefers-color-scheme`; persist.
- **Single source for JS + CSS**: a `tokens.ts` emitting the `@theme` CSS vars *and* exporting raw values, so DOM classes (`text-status-bull`) and chart colours (lightweight-charts `upColor`/cloud fill, passed in JS) come from one definition and re-theme together.

## Layer 2 — Primitives (`ui/src/components/ds/`)
`TileShell` · `DataTable`/`DataRow` (unified list row, ✅ built) · `MetricCell` · `StatusBadge`/`SignalTag` · `StrategyTag` · `Sparkline` · `AccountHeader` · `MetricToggle` · `OrderRow` · `SortHeader` (still just the `mock-v2.html` "Table controls" proposal — Watchlist shipped a gear-icon popover instead, see below, not this primitive) · **`SlideToConfirm`** (✅ built, order submit #108) · **`SwipeAction`** (✅ built, watchlist slide-to-delete #64). Domain tiles become thin wrappers + data wiring.

## Layer 3 — Tile & tab framework (what the primitives enable)
- **Config-driven layouts** — `layouts.ts` is the source of truth; a **view = a read-only tab**; tiles carry per-instance config (validated by a Zod schema). No runtime editor.
- **Tile contract** — every tile = `TileShell` + declared `dataSources` + config schema + registry entry; **uniform data resolution** so tiles work the same way (#29).
- **Tile chrome** — one header pattern (title · summary · actions); consistent **empty / loading / error** per tile (#26, never a silent blank).
- **Navigation / IA** (#106) — **primary tabs** for frequent surfaces + a **☰ drawer of REGISTERED menu items → each opens a tab-set (a "work center")**. Flexible/config-driven (MenuRegistry, same philosophy as tiles/actions) — NOT a hardcoded bolt-on menu. Groups: Trade / Automate / Review / Config. Context-aware detail layouts keyed by **(tab, strategy)** (#70/#71).
- **Responsive reflow** (#60) — tiles reflow desktop→mobile without clipping; the row contract governs list tiles on phones.

## Layer 4 — The row/format contract (STYLE_GUIDE.md)
- **Symbol first** in every list tile · **one main line + one detail sub-line** everywhere.
- **The two-line row is explicit, and the two lines are different kinds of thing:**
  - The **MAIN row** is a normal table row — its cells align to the **column headers** (Symbol · Sig · Last · …).
  - The **SUB-LINE** is a single **full-width `colSpan` cell** under it. It is **free-form and does NOT follow the
    columns**, and the **table header does NOT apply to it** — it's a sentence, not a grid (`strategy · avg · reason`).
  - The sub-line is **ALWAYS VISIBLE** — it is **not an expander/accordion** (no chevron, no tap-to-reveal). Row
    tap opens the **detail surface**, never an inline expand. Terminal dimming / a bracket stripe span **both** lines.
  - So: read the header for line 1, read line 2 as free prose. (Desktop *may* promote sub-line fields into real
    columns per Layer 6 responsiveness — an orthogonal, later concern; the base form is main-row + free-form sub-line.)
- **2 font sizes/row max** (13 data / 10 tag+sub) · **3 greys max**.
- **Colour never alone** — every coloured state carries a text label.
- **Line-2 leads with the primary attribution** — strategy for owned, signal for watch; **drop venue noise**.
- Numbers right-aligned + `tabular-nums`; **P&L tints the cell, not the row**; sparklines watch-only; side = coloured text; **transient tick-flash, not persistent pills**; muted green/red on near-black.
- **Order status is never hidden.** The *lifecycle* state of an order is the most important fact about it, so it must be readable **without horizontal scroll** — never parked as the last column behind an overflow. A **terminal/error** state (`DENIED`, `REJECTED`, `EXPIRED`, `CANCELED`) gets a prominent `StatusBadge` (colour **and** label) and joins the always-visible zone: pinned in the frozen left group next to the symbol, and/or carried on the row's left accent rail so a bad order reads at a glance. **Benign** lifecycle states (`WORKING`/`ACCEPTED`/`FILLED`) stay quiet — a tiny glyph or muted label is fine. A terminal state also surfaces via the Alerts channel (fills/rejects). A "tiny glyph" is acceptable only for benign inline states, never for a reject.
- **Compact labels never wrap** — shorten the word (Capital deployed→Deployed). Long context/status gets its own full-width line, never inside a flex column where `nowrap` shoves the other side off-screen.
- **Frozen identity column** — when a list is wider than the viewport, the **symbol column pins** (`position:sticky; left:0`) so the row identity stays visible while the rest scrolls; on wide desktop lists strategy joins the frozen left group (on mobile it rides the sub-line under the symbol). **For the Orders blotter, status pins with the symbol** — identity + state are the two things that must never scroll away.
- Number formatting: decimals-by-asset, negatives, `$9.8k`/`6.4M`.

## Systematic strategies (#185) — patterns this adds

A systematic strategy decides across a whole pool at once, so its surfaces answer different
questions from the per-symbol manager tiles. Four patterns, all reusable.

- **Provenance chips.** A pool symbol carries *why it is present* — one chip per contributing
  source, plus a chip for an operator override. This is the thing a flat symbol list can never
  show, and it is what makes "add and remove symbols" comprehensible: you can see whether removing
  a name fights a feed that will re-add it. Chips reuse the cloud-chip form (`.cchip`); an override
  chip reads as a `SignalTag`, not a colour alone.
- **Source health belongs in the tile header, and a stale source must be LOUD.** A stale source
  blocks the session's decision, so it is not a quiet grey dot — it reads `STALE 26h — blocks
  decision` in bear/exit. Fresh sources stay quiet. This follows the same rule as order status: the
  state that stops you trading is never allowed to hide.
- **Distance-to-exit, not just P&L.** For a position under a trailing rule the operator's most
  useful number is how much room is left before the exit fires (`to exit +5.9` / `−1.2`), not the
  unrealised percentage. Tint the cell, never the row. A trade that has never been in profit shows
  `—` rather than a number, because a give-back trail only arms in profit — an em dash is a state,
  and it needs the sub-line to say which.
- **Deliberate-but-unusual states get a LABEL, never a colour.** A held name that has left the pool
  is an `ORPHAN`: expected, still managed, never force-sold. Colouring it red would read as an
  error. It carries the tag and a sub-line explaining itself. Same rule as everywhere — colour
  never alone — but the emphasis here is that *not every unusual state is a problem*.

**Which actions are slides.** Entering `TRADING`, liquidating, killing, and blacklisting a symbol
you currently hold — the last one because a blacklist entry is the only way pool membership ever
forces a sale. Whitelisting, clearing an override and pausing stay taps: they cannot lose money or
destroy state.

**One naming rule.** The dry-run state is `SHADOW`, never `ARMED`. `managers.py` already uses ARMED
for "attached and waiting to trigger" (#55); two ARMEDs meaning different things in one engine is a
bug in waiting. `SHADOW` must also read unmistakably as *deciding but not trading* — the whole point
of the state is that it looks alive while being inert.

## Mobile (learned the hard way)
- **`<!DOCTYPE html>`** always — quirks mode breaks `color` inheritance into `<table>` (tables silently theme wrong).
- `text-size-adjust: 100%` on the root — **stop mobile Safari font-boosting wide tables** (identical CSS, different sizes on iOS).
- Tables **fit the viewport** (columns that fit + extras to the sub-line) or degrade predictably; no silent horizontal clip.
- Inputs ≥16px on mobile (no iOS focus-zoom). Verify on a **mobile viewport**, not desktop DOM.

## Safety UX (belongs in the system, not just features)
- **Armed / disarmed** global state as a first-class, always-visible control (surfaces `KUMO_ORDERS_ARMED`; no autonomous BUY).
- **Slide-to-confirm — a tap NEVER commits money or destroys state.** Every *committing* or *destructive* action
  is a **slide gesture**, not a button click: **submit an order (buy/sell/exit), flatten a position, cancel an
  order, delete a watchlist row, disarm, kill-switch**. A `SlideToConfirm` primitive (`components/ds`): a full-width
  track + draggable thumb; the action fires only when the thumb reaches the end (~90%), snaps back if released
  early. Labelled with the exact consequence (`Slide to BUY 190 FIG`, `Slide to FLATTEN CVS`, `Slide to delete`).
  Touch **and** pointer; ≥44px thumb; `prefers-reduced-motion` → falls back to a two-step press-hold-to-confirm.
  Colour by intent (green buy, red sell/flatten/delete). BENIGN actions stay taps (open detail, add to watchlist,
  switch tab). Replaces the current tap-button submit and the watchlist ✕ (which becomes slide-to-delete, #64).
- **Exit is a first-class action, symmetric with entry.** Every held position row (managed **and** unclaimed) and
  the position detail carry an **Exit / Flatten** action (slide-to-confirm) — a market close in RTH, marketable
  limit in extended hours. "I can enter but not exit" is a safety failure: exit must be one gesture, never a
  hand-built opposite-side order.
- **Protective stop on an OPEN position, standalone.** A held position can get / adjust a **Set stop** (protective
  `STOP_MARKET` on the closing side) independent of a bracket-at-entry — a naked position is a risk the cockpit
  must let you cover in one gesture. Surfaced on the position row/detail; slide-to-confirm; shows the resulting
  risk ($ + %).
- **Kill-switch** (STOP ALL: disable auto + cancel working + optional flatten) — itself a slide-to-confirm.
- **Alerts / notifications** surface — fills, rejects, bracket-leg-missing, data-stale, risk-threshold, ALERT-leash proposals (in-app banner/inbox; push later).

## Component spec — `SlideToConfirm`, `SwipeAction`, Exit/Flatten, Set-stop
Buildable contracts for the Safety UX above.

### `SlideToConfirm` (`components/ds/SlideToConfirm.tsx`) — the commit gesture
- **Anatomy:** a full-width **track** (rounded, ≥48px tall) + a **thumb** (≥44px, a chevron/›› affordance) + a
  **label** centred on the track stating the exact consequence (`Slide to BUY 190 FIG`, `Slide to FLATTEN CVS`).
  A **fill** grows behind the thumb as it's dragged (intent colour).
- **Props:** `{ label, onConfirm, intent: "buy"|"sell"|"danger"|"neutral", disabled?, pending?, reason? }`. `intent`
  drives colour (buy=`status-bull`, sell/flatten=`status-bear`, danger=`status-bear`, neutral=`status-info`).
- **Interaction:** pointer **and** touch drag. Fires `onConfirm` **only** when the thumb passes **~90%** of the
  track; released before that → **snaps back** (spring), no fire. While `pending`, the track shows a spinner/`Sending…`
  and is locked. On success the track flashes done then resets; on error it shows `reason` (red) and resets.
- **A11y:** the thumb is a `role="button"` with `aria-label` = the label; **keyboard** = `Enter`/`Space` **arms**
  (shows "press again to confirm") then a second `Enter`/`Space` **fires** (never single-key, so focus+Enter can't
  commit by accident). `prefers-reduced-motion` (and pointer-coarse-less devices) → the same **press-and-hold ~600ms
  to confirm** fallback (a filling ring), no drag required. Respects `disabled`.
- **Never** fires on a plain click/tap. A tap anywhere that isn't the drag/hold is a no-op (or opens detail if the
  row is also tappable — the slide control `stopPropagation`s).

### `SwipeAction` (`components/ds/SwipeAction.tsx`) — swipe-to-reveal on a list row (#64)
- Horizontal swipe on a `DataRow` reveals a trailing action button (e.g. red **Delete**/**Remove** on the watchlist,
  replacing the always-visible ✕). Partial swipe reveals; full swipe past a threshold **arms** the action, which then
  requires a tap on the revealed button **or** a `SlideToConfirm` for destructive ones. Desktop fallback: the ✕ stays
  available on hover. Never auto-commits on swipe alone for anything that places/cancels/deletes.

### Exit / Flatten (action, symmetric with entry)
- **Where:** every HELD position row (managed + unclaimed) + the position detail. A **Flatten** control.
- **Order built:** opposite side, **full held qty**, **market in RTH / marketable-limit in extended hours** (reuse the
  session logic from the ticket). Submitted via the existing `submit_order` command — no new engine API.
- **Confirm:** `SlideToConfirm` intent=`sell`/`danger`, label `Slide to FLATTEN {sym} ({qty})`. Shows the position's
  live P&L on the control so the consequence is explicit.

### Set-stop (protective stop on an open position, standalone)
- **Where:** HELD position row/detail. **Set / adjust stop**.
- **Order built:** a single closing-side `STOP_MARKET` at the chosen trigger (rests at the broker — the standalone
  single-stop path already works; only brackets were emulated). Shows resulting **risk ($ + %)** from avg → trigger.
- **Confirm:** `SlideToConfirm` intent=`danger`, label `Slide to set stop @ {price}`.

## Migration — strangler, no big-bang
1. Tokens (`globals.css` @theme + `tokens.ts`).
2. Primitives + a **live style-guide route** (`/dev/ui` or a `styleguide` tile) rendering every primitive + state.
3. Migrate **Orders first** (reference tile) + enrich its data.
4. Wrap Watch / Positions / Managed-Portfolio / Account onto the primitives.
5. **Enforce**: ESLint bans raw `text-(green|red|emerald)-*` + raw `<table>` outside `DataTable`; PR checklist.

## Surfaces to cover (owning tickets)
Watchlist · Orders (#33) · Managed-Portfolio (#77) · Account header (#41) · Order ticket #51/#67 · Instrument detail #27/#70/#71 · Chart (#58) · states #26/#28/#79 · Home/portfolio-risk (#12/#80) · semi-auto leash+proposal (#55). Depth = **L1/NBBO top-of-book** (Alpaca stocks are L1-only; L2 = crypto-only).

## Layer 5 — Semi-automated trading UX (ref #55)
The semi-auto *feature* is EPIC **#55**. The design system owns the **surfaces + primitives**:
- **Arm-on-fill slot** on the order ticket — pick which automatic actions arm when filled (PEAK · Stop-Reenter · Breakeven · Time-exit · Pyramid); default **all on**, deactivate any.
- **Per-position automation panel** — one **on/off toggle per action**; **arming activates all**; state badge per action (off/armed/watching/triggered). **No approve step, no leash** — on = it runs automatically.
- **Disarm / Kill-all** in the position header · params edit on tap (reuses #49 renderer).
- **Automation event log** per position — what fired (audit, NOT approval).
- **Blotter** shows `AUTO (N)` per name.
- New primitives: `ActionToggle` · `AutomationPanel` · `ManagerBadge` · `EventLog`.

## Layer 6 — Responsiveness (desktop ↔ mobile, first-class)
- **Breakpoints** (mobile/tablet/desktop); spacing + density scale.
- **Board reflow** — tile grid collapses to one column on phone; **tall tiles never clip** (#60).
- **List-row adaptivity** — main+sub IS the mobile form; on desktop sub-line fields **promote to columns**; low-priority columns hide first.
- Tables fit or degrade; ≥44px touch targets; swipe-delete (#64) gets a mobile equivalent.
- Verify on a real mobile viewport.

## Layer 7 — Flexible tile mechanism (Perplexity + code review)
**Today (in code):** `framework/layout/schema.ts` (placed-tile `x/y/w/h` + `schemaVersion`) + `migrate.ts`; `config/layouts.ts` (views = tabs); `datasource/ws-manager.ts` + `useSource.ts` (one WS bus, dedup subs, stale-flagging → uniform data resolution #29); `framework/focus.ts` (context `{tab, strategy_id}` → detail #70/#71); tile `definition.ts` registry.
- **Config-over-editor stays.** 3 schema layers: TileType registry · TileInstance (`id/type/dataSources/config/layout`) · View. Validate all layouts at build/CI; **Layout Playground** dev route (debug overlay, nudge-to-patch); helpers + preset libraries + versioned presets.
- **Tile contract** — declares identity/meta · `configSchema`+`defaultConfig`+`normalize` · data requirements (declarative — *never* fetch/ws in a tile) · layout constraints + mobile hints · `mobileVariant`/`criticalOnMobile`. Tile = pure fn of (config, data, context), returns body only. Framework owns grid/bus/chrome/context/a11y.
- **Responsive** — RGL desktop, CSS grid/flex mobile (bypass RGL on `sm`); own `LayoutSchema` + thin RGL adapter.
- **Context-keyed layouts** — `LayoutContext {tabId, strategyId, symbol, mode}`; field sources `symbolSource: fixed|context`.
- **Pitfalls** — contract drift, ad-hoc fetch, opaque configs, inconsistent chrome, responsiveness-afterthought, perf (memo, dynamic-import charts), non-testable layouts (snapshot each View).

## CLARIFICATION — "flexible tile" = STRATEGY-ADAPTIVE composition (not mobile/responsive)
**Strategy is the top-level context; the whole cockpit is a function of it.** Selecting MANUAL / MOMENTUM / ETF_AUTO re-composes:
- **Tabs** — each strategy has its own tab-group/views.
- **List tiles** — MANUAL discretionary · MOMENTUM momentum candidates + manager-state · ETF_AUTO sleeves.
- **Order screen** — MANUAL ticket · MOMENTUM pick-a-manager/algo · ETF_AUTO rotation (#51 generalized to the whole UI).
- **Detail surface — one per (tab × strategy), registered + swappable — NOT one generic symbol screen.** Every list row taps into a detail specific to BOTH its tab and the active strategy (a `DetailRegistry` keyed by `(tab, strategy_id)`, same pattern as the tile/action registries). The contents differ by tab:
  - **Watch** — chart + Ichimoku signal + cloud-position read + MANUAL order ticket.
  - **Position (Portfolio / Managed)** — live P&L + avg cost/qty, **trade-cycle state** (HELD/ARMED/WATCH), per-leg fills, protective stop/target, close/flatten, chart. **MOMENTUM** adds the semi-auto automation panel (per-action toggles + arm-state + event log); **ETF_AUTO** shows sleeve / rotation context.
  - **Order** — full **lifecycle** (submitted→accepted→working→filled/canceled/**denied**) with timestamps, fills + avg px, bracket/OCO legs grouped, inline modify/cancel, and — critically — **the reject/deny reason in full** (a `DENIED` order must explain WHY here; the user must never have to read engine logs to learn a precision/risk reason).
  - Strategy re-composes each of the above (MANUAL vs MOMENTUM vs ETF_AUTO), so **detail = f(tab, strategy)**.
  **Gaps today:** Order rows have no tap at all; a Position/Portfolio tap opens the symbol-generic surface (chart + ticket), not a position detail. Both to be built as (tab, strategy) detail variants under #70/#71.
All from config keyed by (tab, strategy) (#70/#71). Responsiveness (Layer 6) is a **separate, orthogonal** concern.

## Layer 8 — Component registry + replacement (core architecture)
Everything pluggable is **registered + swappable** by id — the DS exists so all registered components render consistently:
- **TileRegistry** (#7) — register tile types; **swap variants in a slot** (order.vanilla ↔ order.strategy ↔ order.assisted).
- **OrderActionRegistry** (#50) — pluggable entry/exit order types.
- **MechanismRegistry** (#65) — pluggable entry/stop/target derivation.
- **ManagerRegistry** (#55) — pluggable automatic actions (PEAK/pyramid/stop-reenter/readiness).
- **DataSourceRegistry** — pluggable WS streams, bus-bound.
- **DetailRegistry** (#70/#71) — pluggable detail surfaces keyed by `(tab, strategy_id)`: watch / position / order / managed, each strategy-composed. A list row resolves its detail through this, so detail is `f(tab, strategy)` not one generic screen.
- **KpiRegistry** (`@/lib/framework/kpi`, ✅ built #182) — pluggable per-row display metrics (`config/kpis.ts` registers volume, VWAP, today's range, prior close, market cap, beta, EPS, P/E, dividend); a KPI = `{id, label, shortLabel, format, sortValue?}`. First consumer is the Watchlist row's KPI line + gear-icon checkbox list — an unknown/misconfigured id is skipped, never a crash. Same registry pattern as the rest of this layer, not a one-off.

One pattern: `register(descriptor)` → `get(id)` → render/run. New capability = add a descriptor (no core change); **replacement = swap the registered impl by id**.

## Semi-auto meaning + per-position automation panel (Perplexity-reviewed)
**Semi-automated = MOMENTUM, human-selected:** the human picks the stocks + entry (discretionary), then **arms** the position → **all automatic actions activate**; toggle each **ON/OFF** to deactivate/activate. The engine manages the names you chose; it does NOT pick them, and there is **NO approve/skip step and no Auto/Confirm leash** — on = it runs automatically.
UX:
- **One toggle per action** (PEAK sell-spike · stop-and-reenter · move-to-breakeven · time-exit · pyramid-add). **Arm = all on**; deactivate any.
- **State badge per action**: off / armed / watching / triggered / executed (distinct colours; never conflate "configured" with "active").
- **Disarm / Kill-all** in the position header; **params edit on tap** (blotter stays summary-only).
- **Automation event log** per position — what fired (audit; replaces any approval queue).
- **Blotter at-a-glance**: an `AUTO (N)` badge per position.
- Pitfalls: coupling to order-entry only (allow post-entry attach), ambiguous enable-state, hidden/non-invertible automations, exposure-increasing actions (pyramid) ON by default without thought, no per-position audit feed.

## Pending mock surfaces (checklist)
Not-yet-drawn: [x] Board chrome/tabs · [x] Settings (#49) · [x] Search (#25) · [x] Armed/kill/confirm · [x] Alerts · [x] Assisted ticket (#67) · [x] slide-to-delete (#64, shipped + mocked) · [ ] TradingView (#22) note.
Partial→extended: [x] cycle drill-down (#77) · [x] cancel/modify (#33/#34) · [x] correlation+sleeves (#12/#80) · [x] detail-variants (#70/#71) · [x] desktop board note removed (tabs = one surface each) · [x] Watchlist VWAP column + KPI line + gear-icon sort/filter (#182, shipped + mocked).
Deferred: scanner (#45) · BCT-copy (#54) · AI briefs.
