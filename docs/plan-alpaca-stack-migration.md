# Plan — Migrate cockpit bottom layer to Alpaca (data + exec)

> Status: **IN PROGRESS**. Author: session 2026-07-02/03.
> **DECISION (locked 2026-07-03): 100% Alpaca** — data **and** exec, one `providers/alpaca.py`, one
> instrument namespace (no #17 collision), static key (no gateway/2FA). Keep `databento.py` + `ibkr.py`
> registered-but-dormant as insurance. Rationale: the operator trades live on IBKR → IBKR data permanently
> contended (162); Databento live = $200; Alpaca = cheap + unattended + SG-confirmed + one vendor.
> Escape hatch: if the cockpit ever must execute into the *live IBKR* account, flip exec back to `ibkr`
> via the registry (config change, not a rebuild).
> Supersedes the in-flight IBKR-data-client experiment (see "Current repo state" below).

## Transition / cutover sequence (safe, reversible)

1. **Lock in a working fallback first.** On the current branch, commit the two keeper fixes (`config.ts`,
   compose port) and set `feed.toml provider = "databento"` so `main` always has a *working* baseline
   (chart + positions render). This is the rollback target.
2. **Branch** `feat/alpaca-stack` off that clean baseline.
3. **Build additively** — add `providers/alpaca.py` + register it; the databento/ibkr adapters stay.
   Nothing switches yet; the baseline still runs.
4. **Switch by config** — `feed.toml` `[data].provider` + `[execution].provider` → `alpaca`. Test on
   paper (IEX data). **Rollback = flip those two lines back to databento/ibkr** (adapters still present).
5. **Prune** the IBKR gateway infra only *after* Alpaca is proven on paper (compose gateway service,
   socat, 2FA, keychain gateway creds). Keep `ibkr.py`/`databento.py` code.
6. **Verify (market hours) → PR → merge.**

Rollback at any point = one config flip; nothing is deleted until step 5, and even then only the
gateway *infrastructure*, never the adapter code.

## Why (the constraint that drives this)

- **the operator trades live on IBKR.** IBKR market data is licensed **per-user, one active session at a time**. The live trading session permanently holds the entitlement, so the cockpit's IBKR data client is **always contended** → persistent error **162 "connected from a different IP"** + empty bars. Confirmed this session.
- **Databento** is native + unattended + SG-friendly, but its **live** license is ~$200/mo (current plan is historical-only → static prices).
- **Alpaca** = API-first broker + data. **Static API key, no gateway, no 2FA, no daily login** (unattended), one vendor for **data + execution + enrichment** (news/movers/corp-actions), SG-usable (verify account eligibility). Downsides: **no native Nautilus adapter** (RFC [#3374](https://github.com/nautechsystems/nautilus_trader/issues/3374) still open) → we build one (~1–2k LOC); real-time needs the paid **SIP** tier (~$99/mo), free tier is delayed/IEX-thin.

**Decision shape:** keep the **Nautilus engine** and the **Next.js cockpit UI**; swap only the **bottom layer** (broker + data) to Alpaca via a new adapter. This is what the provider registry was built for ("broker swap never loses data").

## Guiding principles

- **Add + switch + prune — NOT rebuild.** Engine, UI, tile framework, WS bridge, provider registry all stay unchanged.
- **Keep `databento.py` + `ibkr.py` adapters** behind the registry — they cost nothing and are the vendor-swap insurance. Do **not** delete them.
- **Prune the IBKR *gateway infrastructure*** (gnzsnz gateway, socat, 2FA/login, keychain gateway creds) — that's the pain Alpaca removes.
- **Price rides Nautilus; enrichment rides "on top"** (FastAPI → Alpaca REST → `rest` tiles). Do not extend Nautilus to model news/movers.

## Prerequisites (before Phase 1)

- [x] **Alpaca account usable from the operator's jurisdiction — CONFIRMED 2026-07-03.** Paper account `ACTIVE` ($100k cash), trading API + IEX data both reachable from SG on the paper key. SG eligibility risk cleared.
- [x] **Paper API key stored in keychain** — services `alpaca-paper-key` (Key ID, `PK…`) + `alpaca-secret-paper` (Secret). Adapter reads via Alpaca-standard env vars `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`. (Paper key is in the 2026-07-03 chat transcript → rotate in the Alpaca dashboard after wiring.)
- [ ] Decide data tier: **free IEX** (thin/delayed — proven working) for dev, **SIP $99/mo** for full real-time. Alpaca = 1 concurrent market-data connection per key; keep the cockpit key separate from tradex/other use.

## Phase 1 — Alpaca **data** adapter (Nautilus, Python)

New: `backend/api/providers/alpaca.py`
- `AlpacaInstrumentProvider` — build `Equity` instruments from Alpaca `/v2/assets` (US equities/ETFs), MIC-venue mapped to match the cockpit's `TICKER.MIC` ids.
- `AlpacaDataClient` (subclass Nautilus `LiveMarketDataClient`): implement the equity subset —
  `_connect`/`_disconnect` (WS auth handshake), `_subscribe_bars`/`_subscribe_trade_ticks`/`_subscribe_quote_ticks` (+ unsub), `_request_bars` (REST historical), `_request_instrument(s)`. Skip order-book/derivatives/funding methods.
- Config `AlpacaDataClientConfig` (api_key_env, secret_env, feed=`iex|sip`, base_url) + factory.
- Parsing: Alpaca JSON → Nautilus `Bar`/`QuoteTick`/`TradeTick` (precision, ts_event ns, aggregation source).
- **Reconnection + resubscribe** on WS drop (Alpaca 1-connection limit; backoff).

Edit:
- `backend/api/providers/__init__.py` → register `"alpaca": alpaca.build_data`.
- `backend/config/feed.toml` → add `[data.alpaca]` (feed tier, key env names); set `[data].provider = "alpaca"`.
- `backend/api/bar_spec.py` → **note:** Alpaca streams **1-minute** bars natively over WS (unlike IBKR's 5-sec-only). So `1-MINUTE-LAST-EXTERNAL` live subscription should work directly — verify against the live WS; keep 1h/1d as historical + internal aggregation if needed.

References: `tradex` Elixir `alpaca_bars.ex`/`mapper.ex` (endpoints, auth, bar normalization), Nautilus `adapters/_template/` + `adapters/databento/` (structure), RFC #3374 (the planned native design).

**Verify:** engine connects on a static key (no gateway); historical bars load; **live 1-min bars flow during market hours → prices move in the UI**; reconnection survives a forced WS drop.

## Phase 2 — Alpaca **execution** (single-vendor; optional but recommended)

New in `alpaca.py`: `build_exec()` → `ExecClientSpec` (`AlpacaExecClient`): order submit/cancel, position/account reconciliation.
Edit: `feed.toml [execution].provider = "alpaca"`.
Keep `ibkr.py` exec adapter registered (unused, insurance).

**Verify:** paper positions/account reconcile from Alpaca into the cache → portfolio tile renders them.

## Phase 3 — Prune IBKR gateway infrastructure

- `deploy/compose.paper.yml` + `compose.prod.yml`: remove the `ib-gateway` service, the `broker` network, and `KUMO_IBG_*` env. Add `ALPACA_*` env.
- Retire `backend/scripts/ib_gateway.py` + the socat/2FA/keychain-gateway machinery.
- `.env.*`: drop `TWS_USERID`/`TWS_PASSWORD`/`IBKR_ACCOUNT_ID`; add `ALPACA_API_KEY`/`ALPACA_API_SECRET`.
- **Keep** `providers/ibkr.py` (registry insurance).

**Result:** the entire 162 / socat-port / 2FA / daily-restart pain **deletes itself**. Static key only.
**Verify:** `make up-paper` brings up redis + engine + api + ui with **no gateway container**.

## Phase 4 — Enrichment tiles (additive, "on top", separate lane)

- `backend/api`: thin FastAPI routes proxying Alpaca REST — `/movers`, `/news`, `/corporate-actions`.
- `ui`: new `kind: "rest"` datasources + tiles (Movers, News, an earnings/ex-div badge on portfolio rows).
- This lane is **Alpaca-coupled** by design; it does NOT ride the Nautilus bus.

**Verify:** movers/news tiles render live; portfolio rows show earnings/ex-div warnings.

## Phase 5 — Verify → PR → merge

- End-to-end paper run **during market hours**: prices move, positions reconcile, enrichment tiles populate, reconnection holds.
- Codex/`/code-review` pass on the branch.
- **PR → review → merge to main.**

## Git process

```
git checkout main && git pull
git checkout -b feat/alpaca-stack
# Phase 1..4 as atomic commits
# verify on paper
gh pr create ...   # review, then merge
```
Do **not** delete the databento/ibkr adapter code. Prune only the gateway *infrastructure*.

## Risks / open items

- **No native adapter** → build + maintain against Nautilus API drift. **Watch RFC #3374** — if a native Alpaca adapter lands, discard the custom one and switch (registry makes this a config change).
- **Alpaca 1 concurrent MD connection per key** → dedicated cockpit key; don't share with tradex.
- **Free IEX = delayed/thin (~3% volume)**; real-time = **SIP $99/mo**.
- **Account eligibility in the operator's jurisdiction** — verify before investing in the adapter.
- Historical pacing / rate limits (Alpaca 10k calls/min — generous).

## Effort estimate

| Phase | Effort |
|---|---|
| 1 — data adapter | ~4–6 focused days |
| 2 — exec adapter | ~2–3 days |
| 3 — prune gateway | ~0.5 day |
| 4 — enrichment tiles | ~1–2 days |
| 5 — verify/PR | ~1 day |

## Decision gates

1. **Before Phase 1:** confirm Alpaca works from SG + pick data tier. If SG-blocked → fall back to **Databento live ($200)** (native, zero adapter work).
2. **After Phase 1:** if RFC #3374 native adapter has landed → adopt it instead of the custom adapter.

---

## Current repo state (session 2026-07-02/03) — reconcile before starting

Working tree (uncommitted) on branch `feat/7-tile-framework-core`:
- ✅ **KEEP** `ui/src/lib/config.ts` — real fix: empty-string `NEXT_PUBLIC_*` baked by Docker defeated `??`; now length-guarded. This is what made the UI render data at all. Verified.
- ✅ **KEEP** `deploy/compose.paper.yml` — gateway port `4002`→`4004` (gnzsnz socat). Verified. (Becomes moot once the gateway is pruned, but correct until then.)
- 🔧 **WIP / decide** — the IBKR **data-client** experiment: `backend/api/providers/ibkr.py` (`build_data`), `providers/__init__.py` (registered `ibkr` data), `engine_node.py` (provider-agnostic window), `config/feed.toml` (`provider = "ibkr"`, `[data.ibkr]`). This proved IBKR data works *mechanically* but is blocked by the 162 entitlement lock (the operator trades live on IBKR). **The Alpaca plan replaces this.** Options: (a) revert these four files to Databento baseline now (restores working chart+positions), or (b) leave as reference until the Alpaca branch starts.
- `.env.paper` currently points at the paper login — reverted from the `<secondary-user>` secondary-user test (its headless login hung on a 2FA/first-login dialog).

Recommended immediate action: **revert the 4 IBKR-data-experiment files to `provider = "databento"`** so `main` (via this branch) keeps a *working* baseline (historical chart + positions), then do Alpaca on a fresh branch.
