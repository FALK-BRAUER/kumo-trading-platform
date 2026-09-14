# Tile Framework — Build Plan & Loop Protocol (#7)

Operating manual for the autonomous build loop. The loop re-reads this file each wake; it is the
source of truth for *what's next* and *what counts as done*. Architecture rationale: `ui-framework-spec.md`
+ issue #7.

## ⚠️ SCOPE CUT (Operator, 2026-06-29)
**No runtime layout editor.** Layouts are JSON/TS config (`@/config/layouts`), edited by hand. The
P4 edit-mode (draft drag/resize, palette, ✕) and ALL of P5 (backend `/layouts` JSON-file persistence,
load/migrate-from-server, runtime view CRUD, optimistic save/delete) were built, reviewed — then
**deleted** at the operator's call after seeing the app. **Kept:** tile registry, DataSource registry + WS
multiplex (the live data plane), grid render-from-config (RGL read-only), read-only view tabs (switch
between config-defined views). P6 (Ichimoku/Order tiles) unchanged. History below is retained for the
record; the editor/persistence sections are no longer live.

## Loop protocol (full-autonomous, phase-bracketed)

Each **phase** is bracketed: **Perplexity plans it → steps run → Codex reviews it.** Both brackets are
automated (no human) — the loop only stops at ⚠️ gates, a red verification, or a blocking Codex finding.

### Per-phase bracket
1. **Plan (Perplexity)** — before a phase's first step, query Perplexity (`perplexity_ask`) for the
   phase's design/approach given current code. Refine the phase's step list from the answer; write a
   2–4 line **Plan note** under the phase header and commit it (`docs(plan): P# plan note`). Tick `Plan`.
2. **Steps** — run the phase's steps per the iteration loop below.
3. **Review (Codex)** — when all the phase's steps are checked, run
   `codex exec review --base <phase-start-sha>` (the sha before step 1 of the phase). Record the verdict
   under the phase header. Address **actionable** findings in a follow-up commit (`fix(ui): address Codex P# review`)
   and re-run the gate. **Blocking** findings (correctness/security/contract break) the loop can't safely
   auto-fix → **STOP**, surface to the operator. Tick `Review`. Then advance to the next phase's Plan.

### Step iteration (within a phase)
1. Read this file. Pick the **first unchecked step** that is **not** ⚠️ NEEDS-DECISION.
2. Implement only that step. Small, atomic diff.
3. **Verify (the gate — all three must pass):**
   - `cd ui && npx tsc --noEmit`
   - `cd ui && npm run build`
   - `cd backend && .venv/bin/python -m pytest -q`
4. **Green** → conventional commit (`Refs #7`, co-author trailer) + tick the box `[x]`.
   **Red** → one fix attempt. Still red → **STOP**, report, do not commit broken state.
5. Next step ⚠️ NEEDS-DECISION → **STOP**, surface the decision.
6. Else continue to the next step (or the phase Review if steps are done).

### Stop conditions
- All phases reviewed → loop complete, report + offer PR.
- Gate red after one fix attempt.
- Next actionable step is ⚠️ NEEDS-DECISION.
- Codex flags a blocking finding the loop can't safely auto-resolve.

### Guardrails (non-negotiable)
- Never push or merge without explicit ask. Never commit to `main`. Work stays on `feat/7-tile-framework-core`.
- All new automation flags/gates default `False`. No secrets, ever. Paper only.
- UI render-only: tiles never fetch; no trading logic in the UI.
- Keep every new non-hidden dir's README current.
- Don't expand a step's scope. If a step reveals new work, add a checklist item — don't fold it in silently.

---

## P2 — DataSource registry (REST first)

> Bracket: `[x] Plan (Perplexity)` · `[x] Review (Codex)` — verdict: no actionable issues · phase-start-sha: `9a1b957`
> Plan note (Perplexity 2026-06-27): `useSource(name,cfg)` switches on `def.kind` INTERNALLY — tiles
> only ever see `name` + the `TileStatus` enum, so the WS swap next phase is invisible. Container uses
> `useSources(names,cfg)` (loops the static `dataSources` array — rules-of-hooks safe since length/order
> are stable per tile type). Query key = `['source', name, params(cfg)]` — WS phase reuses the SAME key
> so `setQueryData` hits the same cache entry (`kind:'hybrid'` = REST bootstrap + WS freshen). Per-source
> `cacheMs`→`staleTime` (+ optional `gcMs`→`gcTime`); no global staleTime. `TileStatus` mapping lives in
> ONE place (container/useSources), tiles never touch TanStack `is*` flags. Provider: singleton
> QueryClient scoped at the board root (client). Defaults if a source omits them: stale 2s, gc 60s.

- [x] **2a** DataSource core types + registry. `src/lib/framework/datasource/registry.ts`: `DataSource`
      type (`name`, `kind: "ws"|"rest"|"hybrid"`, `topic?`, `endpoint?`, `params?(cfg)`, `cacheMs?`, `gcMs?`)
      + register/get. `TileStatus` already = `SourceStatus` in types.ts. Commit: `feat(ui): DataSource registry core`.
- [x] **2b** QueryProvider + `useSource`/`useSources`. Install `@tanstack/react-query`; singleton
      QueryClient provider wrapping the board; `useSource(name,cfg)` delegates by `def.kind` (REST path:
      `useQuery` keyed `['source',name,params]`, `staleTime=cacheMs??2000`, `gcTime=gcMs??60000`);
      `useSources(names,cfg)` loops the static list → `{data,status}` with a single `mapToTileStatus`.
      Commit: `feat(ui): useSource/useSources (REST via TanStack Query)`.
- [x] **2c** Container wires dataSources → props. `TileContainer` calls `useSources(def.dataSources, config)`,
      passes `data[name]`/`status[name]` down. PortfolioTile reads `data.positions`, drops its `getPositions`
      call. Commit: `feat(ui): container injects resolved data into tiles`.
- [x] **2d** `src/config/datasources.ts` — register `positions` (REST `/positions`, `kind:"rest"`). README for
      `datasource/` dir. Commit: `feat(ui): register positions REST source`.

## P3 — Multiplexed WS + backend topic protocol

> Bracket: `[x] Plan (Perplexity)` · `[x] Review (Codex)` · phase-start-sha: `1f5b244`
> Codex verdict (2026-06-28): 2 actionable, 0 blocking. (1) backend unsubscribe leaked the in-flight
> live-tail task → dup/out-of-order bars on quick resubscribe — now `live_tasks` is keyed by topic and
> cancelled on unsubscribe + resubscribe (regression test `test_ws_unsubscribe_cancels_live_tail`).
> (2) dropping `useStream` left ChartTile fills `[]` → BUY/SELL markers vanished — registered a shared
> ref-counted `fills` WS source; `PositionRow` filters to its instrument and passes them. Fix commit below.
> Plan note (Perplexity 2026-06-27) — ⚠️ wire format AWAITING A DECISION (3a). Perplexity's full spec:
> structured envelope `{type:event, event:data|status|error|control_ack|heartbeat, topic:{channel,params},
> payload:{frame_type,data}}`, structured topics, per-topic replay_start→snapshot→replay_end gates
> (server must not emit increments before replay_start — kills the race), client-side refcount (one
> wire subscribe per topic), resubscribe-all on reconnect, canonical topic key = `channel:sorted(params)`.
> DECIDED 2026-06-27: **full structured envelope + structured topics.** Wire:
> client→server `{type:'control', op:'subscribe'|'unsubscribe'|'ping', topic:{channel,params}}`;
> server→client `{type:'event', event:'data'|'status'|'error'|'control_ack'|'heartbeat', topic?,
> payload:{frame_type:'snapshot'|'bar'|'fill'|'status', data}}`. Per-topic gate: control_ack(ok) →
> replay_start → snapshot → replay_end → increments (server MUST NOT emit increments before
> replay_start). Errors: control_ack{status:'error',error} + an `event:'error'` frame → tile 'error'.
> Heartbeat: server `event:'heartbeat'` (no topic) + rely on WS ping. Canonical topic key (client
> refcount + query cache) = `channel:` + sorted `k=v` params. Client: WS manager outside React,
> refcount map, reconnect+backoff, resubscribe-all on reopen, exposed via useSyncExternalStore;
> `useSource` ws path subs on mount / unsubs on last unmount.

- [x] **3a** NEEDS-DECISION — backend WS subscribe/unsubscribe wire format. Decision: frame shape for
      `{op:"subscribe", topic:"bars:AAPL.XNAS"}` etc., topic naming, and snapshot-on-subscribe semantics.
      Then implement topic router on `/ws/stream` + pytest. Commit: `feat(backend): WS topic subscribe protocol`.
- [x] **3b** WS manager (outside React). `src/lib/framework/datasource/ws-manager.ts`: one multiplexed
      conn, `Map` keyed by source-key, `refCount`, reconnect + backoff, resubscribe-all on reopen.
      Commit: `feat(ui): multiplexed WS manager`.
- [x] **3c** `useSource` WS path via `useSyncExternalStore` reading the WS manager cache; ref-counted
      subscribe/unsubscribe on mount/unmount. Commit: `feat(ui): useSource WS path`.
- [x] **3d** PortfolioTile bars via `data.bars` (param'd by instrument); remove `useStream`. Delete
      `src/lib/ws/useStream.ts` if unreferenced. Commit: `refactor(ui): portfolio bars via DataSource`.
- [x] **3e** Backend: emit live incremental bar/fill frames over subscribed topics (supersedes the
      snapshot-then-idle handler — the deferred live-push). pytest covers a streamed frame. Commit:
      `feat(backend): live bar/fill frames over WS topics`.

## P4 — react-grid-layout

> Bracket: `[x] Plan (Perplexity)` · `[x] Review (Codex)` · phase-start-sha: `be7de34`
> Codex verdict (2026-06-28): 4 actionable, 0 truly blocking (the [P1] "build will fail" claim was
> wrong — `next build` passed; package CSS imports from a client component are allowed in App Router).
> Fixed all 4: (1) relocated RGL/react-resizable CSS to `app/layout.tsx` (convention); (2) `OrderModal`
> now portals to `document.body` — a `fixed` modal under a grid-item CSS transform was trapped in the
> tile, not the viewport; (3) `minW/minH` from each tile's `minSize` carried into the RGL layout so
> resize can't go below the usable minimum; (4) `updateDraftFromRgl` only flips `draftDirty` on a real
> geometry change (RGL's fires-on-mount no longer marks a fresh draft unsaved). Fix commit below.
> Plan note (Perplexity 2026-06-28): single-breakpoint cockpit → `WidthProvider(GridLayout)` (not
> Responsive). Geometry is user-owned: `compactType={null}` + `preventCollision={false}` (honor exact
> x/y/w/h, items shift not auto-pack). Pure converters `tilesToRglLayout` (→`{i:instanceId,x,y,w,h}`)
> / `applyRglToTiles` (write back geometry only, match on instanceId — `key===i===instanceId`, never
> index). SSR: `dynamic(()=>import(impl),{ssr:false})`, `loading` = fixed blank box (no hydration
> mismatch), `measureBeforeMount={false}`. Draft state = a SEPARATE `draftLayout: Layout|null` slice
> field (not cloned into `layouts` until save) → discard = drop draft. `onLayoutChange` guarded
> `if(!editMode)return` (kills the fires-on-mount feedback loop); writes only to draft. Render source =
> `editMode&&draft ? draft : activeLayout`. Add = `addTileToDraft` (palette → default config, fresh
> `crypto.randomUUID()` instanceId); remove = ✕ overlay → `removeTileFromDraft`. In-memory commit on
> edit-exit for now; real `PUT /layouts` is P5 5d.

- [ ] **4a** Install `react-grid-layout` + `@types/react-grid-layout`; import RGL+react-resizable CSS;
      `GridShell` = `dynamic(()=>import('./GridImpl'),{ssr:false})` with a fixed blank `loading` box;
      `GridImpl` wraps `WidthProvider(GridLayout)` (cols/rowHeight/compactType=null/preventCollision=false,
      `measureBeforeMount={false}`), props pass-through. No Board wiring yet. Commit: `feat(ui): RGL grid shell (client-only)`.
- [ ] **4b** Pure converters `tilesToRglLayout`/`applyRglToTiles`. Board renders active view's
      `PlacedTile[]` through `GridShell` by geometry, `isDraggable/isResizable={false}` (read-only),
      `key===instanceId`, `data-grid` per item. Replaces the flow column. Commit: `feat(ui): render tiles on the grid by geometry`.
- [ ] **4c** Store: add `draftLayout` + `enterEditMode`(clone active)/`discardDraft`/`commitDraft`/
      `updateDraftFromRgl`; `toggleEdit` enters/commits. Board source = draft in edit else active; grid
      draggable/resizable in edit; `onLayoutChange` guarded → draft; dirty flag; Discard reverts.
      Commit: `feat(ui): grid edit mode (draft drag/resize)`.
- [ ] **4d** Add/remove: palette of registered tile types (edit-mode toolbar) → `addTileToDraft`
      (default config, `crypto.randomUUID()` id, placed at y=∞ so it lands at the bottom); ✕ overlay per
      tile in edit → `removeTileFromDraft`. Commit: `feat(ui): add/remove tiles in edit mode`.

## P5 — Views + persistence

> Bracket: `[x] Plan (Perplexity)` · `[x] Review (Codex)` · phase-start-sha: `39483ae`
> Codex verdict (2026-06-29, gpt-5.5 xhigh): 2 actionable, 0 blocking. (1) tab add/rename persisted to
> the store only → lost on reload; now ViewTabs PUTs the new/renamed view immediately (shared
> `cacheUpsert`). (2) failed Done discarded the draft + hid the error (commitDraft ran before the save
> resolved); now the save commits + exits edit ONLY on success — a failed PUT keeps the draft + edit
> mode, shows "save failed — retry", Done/Discard disabled while saving. (Note: the first review process
> hung ~7h on a stuck API call; killed + re-ran clean.) Fix commit below.
> ⚠️ 5a DECISION (the operator 2026-06-29): **JSON file on disk** (not SQLite, not in-memory) + **per-layout
> DTO** — `GET /layouts` lists, `PUT /layouts/{id}` upserts one. Single implicit user (no auth yet).
> Plan note (Perplexity 2026-06-29): backend `storage.py` = temp-file + `fsync` + `os.replace` (atomic,
> corruption-safe) guarded by an `asyncio.Lock` (serializes read-modify-write — concurrent PUTs else
> drop updates), file ops via `run_in_threadpool` (keep async routes off blocking IO). Pydantic v2
> `Layout`/`PlacedTile` with `config: dict[str,Any]`; PUT 400s if path id≠body id. Missing file → GET
> returns `[]`; UI seeds its own `DEFAULT_LAYOUTS` when the fetch is empty and persists on first save
> (backend stays dumb, defaults are a UI concern). Frontend: `useQuery` `staleTime:Infinity` +
> `refetchOnWindowFocus:false`, hydrate the store ONCE via `useEffect` on `data` gated by an
> `initialized` flag (v5 `useQuery` has NO `onSuccess`); migrate each layout client-side; empty→seed.
> Edits mutate Zustand only; `PUT /layouts/{id}` optimistic mutation clears the dirty flag + updates the
> query cache on success; NEVER rehydrate store from a refetch after init (clobbers in-progress edits).

- [x] ⚠️ **5a** ✅ Backend JSON-file layout store. `api/storage.py` (atomic temp+fsync+replace, asyncio.Lock,
      threadpool) + Pydantic `Layout`/`PlacedTile`/`LayoutUpsert`; routes `GET /layouts`→`list[Layout]`,
      `PUT /layouts/{id}` upsert (path/body id match). pytest: empty→[], upsert→readback, id-mismatch 400.
      File path env-overridable, gitignored. Commit: `feat(backend): JSON-file /layouts persistence`.
- [x] **5b** Fetch layouts on load (Query, staleTime Infinity), `migrateLayout` each, seed the store once
      via `useEffect`+`initialized` (empty → `DEFAULT_LAYOUTS`). Replaces the unconditional seed effect.
      Commit: `feat(ui): load + migrate persisted layouts`.
- [x] **5c** View tab strip — active view switch, add/rename/delete view (store actions + UI). Commit: `feat(ui): view tabs`.
- [x] **5d** Save → `PUT /layouts/{id}` (optimistic mutation); `commitDraft` triggers the save; dirty
      clears on success, rolls back on error. Commit: `feat(ui): persist layout edits`.
- [x] **5e** (surfaced by 5c) Persist view DELETE. The per-layout DTO is GET-list + PUT only, so a
      deleted view reappears on reload. Add backend `DELETE /layouts/{id}` + pytest, allow DELETE in
      CORS, and call it from `deleteView`. Commit: `feat(backend): DELETE /layouts/{id}` + `feat(ui): persist view delete`.

## P6 — Catalog port (each ⚠️ — visual correctness needs the operator)

> Bracket: `[ ] Plan (Perplexity)` · `[ ] Review (Codex)` · phase-start-sha: _(record at plan time)_
> Plan note: _(filled by the loop)_

- [ ] ⚠️ **6a** NEEDS-DECISION — Ichimoku tile: `cfg.ticker`, bound to an `ohlcv`/`bars` source; port the
      existing ChartTile into a registered tile. Visual check. Commit: `feat(ui): ichimoku tile`.
- [ ] ⚠️ **6b** NEEDS-DECISION — Order tile: `cfg.ticker` prefill; submit stays inert until #5. Commit:
      `feat(ui): order tile`.
- [ ] **6c** (later) account · watchlist · risk · lanes · bct — out of scope until the above ship.
