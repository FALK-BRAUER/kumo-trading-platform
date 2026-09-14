# Spec — systematic strategy runtime (symbol pool · lifecycle · action log)

**Status:** draft, standalone. Not yet merged into the semi-auto epic (#55) or the MOMENTUM lane (#9).
**Scope:** what the cockpit needs so a *systematic, unattended* strategy can run on paper, be watched,
and be steered. The first tenant is the momentum rotation in `kumo-strategies` (backtested: +62.5%,
Sharpe 3.10, out-of-sample Sharpe 2.89 over 167 sessions).

---

## 1. Why this is not covered by what we have

The existing specs assume a **discretionary or semi-auto** flow: a human reviews candidates (#54),
arms a manager per symbol (#55), confirms or is alerted (#46). That is the right model for
single-name trading.

A systematic strategy is different in kind. Once per session it ranks a pool, picks a target book,
and rebalances — across many names at once, without a human in the loop. Nothing in the current
corpus covers that:

| need | nearest existing spec | why it does not fit |
|---|---|---|
| pool from an external feed | #54 BCT-copy source | specced as *candidates to review and confirm*, one at a time |
| momentum strategy | #9 MOMENTUM lane | specced as semi-auto per-symbol managers, not a portfolio rule |
| trailing exit | #46 PEAK algo | close cousin of our give-back rule — see §7, we should converge |
| audit log | #110 journaling | trade-level journal, not decision-level provenance |
| add symbols | #25 symbol search | finds instruments; does not manage a pool |

**Correction to #55.** Its text says *"No autonomous placement — the leash is the human gate."*
That is stale. Arming **is** the gate: an armed strategy places orders without per-action
confirmation. This spec assumes that, and #55 should be updated rather than contradicted.

---

## 2. Symbol pool — layered, derived, never a flat list

The pool has multiple contributors: some automatic (refresh nightly, add *and* remove), one manual.
Storing a single mutable set makes those fight. Store contributions; derive the pool.

```
  SOURCES (automatic, machine-owned)
    george_book        nightly CSV from fintrack
    industry_members   ETF constituents
    scanner            #45 output
      each owns its own set · replaced WHOLESALE on refresh · never hand-edited

  OVERRIDES (manual, operator-owned)
    PIN      always in, even when no source contributes it
    EXCLUDE  always out, even when a source keeps re-adding it
      explicit rows carrying reason · who · when

  EFFECTIVE POOL  =  union(source sets)  ∪  pins  −  excludes
      derived on read · never the stored source of truth
```

Every conflict resolves without ambiguity:

- feed drops a name you pinned → the PIN keeps it; a source cannot delete what it does not own
- you remove a name the feed re-adds → the EXCLUDE holds; no whack-a-mole
- a source errors or goes stale → its last good set stands, and staleness is **visible**, not a
  silently emptied pool

Because a refresh **replaces a source's whole set** rather than emitting add/remove deltas, imports
are idempotent: re-running last night's file twice changes nothing.

### 2.1 The rule that prevents an accident

> **Pool membership governs BUYING only. It must never force a SELL.**

A held name whose source drops it stops being a buy candidate and stays under the exit rule. Without
this, an upstream CSV glitch liquidates the book at market. We have already been bitten from the
other direction: a pool-expiry bug evicted names the trader still held, and the strategy sold his
winners ~50 days early — a 24-point drag on return.

Only an explicit operator EXCLUDE may force liquidation, and only behind a confirmation.

### 2.2 Schema

```sql
-- one row per (source, symbol), rewritten wholesale per refresh
symbol_pool_source(
  source        text not null,          -- 'george_book' | 'scanner' | ...
  symbol        text not null,
  meta          jsonb,                  -- source-specific: rank, opened_at, score
  refreshed_at  timestamptz not null,
  primary key (source, symbol))

symbol_pool_refresh(                    -- health, so staleness is observable
  source        text primary key,
  refreshed_at  timestamptz not null,
  status        text not null,          -- ok | stale | failed
  symbol_count  int not null,
  detail        text)

symbol_pool_override(
  symbol        text not null,
  kind          text not null,          -- 'pin' | 'exclude'
  reason        text,
  created_by    text not null,
  created_at    timestamptz not null,
  expires_at    timestamptz,            -- null = until removed
  primary key (symbol, kind))
```

Effective pool is a view. Provenance — which sources contribute a symbol, which override applies,
when each source last refreshed — comes free and is what the UI renders.

---

## 3. Strategy lifecycle

Today strategies are **startup-only**: `engine_node.py:2664` registers exactly one, and
`backend/strategies/` is empty. Runtime control is the largest single gap.

```
  DISABLED ──enable──▶ WARMUP ──history ready──▶ ARMED ──operator──▶ TRADING
     ▲                                             ▲                    │
     │                                             └──pause─────────────┤
     │                                                                  │
     └── LIQUIDATING ◀──flatten── HALTED ◀──risk breach / disconnect ────┘
```

| state | places orders? | holds positions? | entered by |
|---|---|---|---|
| `DISABLED` | no | no | default |
| `WARMUP` | no | no | operator enable; needs N sessions of history |
| `ARMED` | no | yes | automatic when warm — computes and publishes decisions, submits nothing |
| `TRADING` | **yes** | yes | **operator only** |
| `HALTED` | no | yes | **automatic** on risk breach or data loss; operator may also force |
| `LIQUIDATING` | exits only | draining | operator only |

Two properties matter more than the diagram:

- **ARMED is the dry-run state.** It runs the full decision path and publishes what it *would* do,
  without submitting. This is how the strategy earns trust before it trades, and how a config change
  is validated. Nothing else in the cockpit has this today.
- **Only an operator may enter TRADING.** Everything can stop it automatically; nothing can start it.

---

## 4. Action log — decisions, not just orders

Orders answer *what*. The log has to answer **why**, months later. One append-only table, written by
the engine, never updated.

```sql
strategy_action_log(
  id           bigserial primary key,
  ts           timestamptz not null,
  strategy_id  text not null,          -- 'MOMENTUM-001'
  session      date not null,
  kind         text not null,          -- decision | order | fill | state | pool | risk | error
  symbol       text,
  summary      text not null,          -- one human-readable line
  detail       jsonb not null,         -- the evidence
  correlation  text)                   -- ties decision → order → fill
```

A `decision` row carries the whole basis: the ranked candidates with scores, the gate that blocked
each rejected name, the target book, and a per-symbol reason (`entered: rank 3`,
`exited: gave back 52% of peak`, `held: rank 5 within buffer`). That is what makes
*"why did it buy STX on 1 April"* answerable without re-running anything.

Retention: keep `decision` and `order` forever (small — one decision row per session), trim
`state` heartbeats after 90 days.

---

## 5. Scheduling

No once-per-session job exists; timers today are interval-based (`engine_node.py:378`, manager
dispatch every 30s). Needs:

- a **session-aware scheduler** firing at a market-relative offset (e.g. open+5m), not wall-clock,
  honouring the exchange calendar and half-days
- **idempotent by `(strategy_id, session)`** — a restart mid-session must not double-trade. The
  action log is the idempotency key.
- pool refresh runs **before** the decision, and a failed or stale refresh is a hard block on
  trading that session, not a silent skip

---

## 6. API and UI surface

```
GET    /strategies                        catalog + state + last decision
POST   /strategies/{id}/state             {state, reason}  → operator transitions
GET    /strategies/{id}/decisions?from=   decision history
GET    /strategies/{id}/log?kind=&from=   action log, filterable

GET    /pool                              effective pool + provenance per symbol
POST   /pool/override                     {symbol, kind: pin|exclude, reason}
DELETE /pool/override/{symbol}/{kind}
POST   /pool/refresh/{source}             force a source refresh
GET    /pool/sources                      per-source health + staleness
```

New WS topics alongside the existing twelve (`app.py:69`): `strategy_status`, `decision`,
`action_log`, `pool`.

**UI — one Strategy tile group:**

| panel | shows | controls |
|---|---|---|
| Status | state · session P&L · positions · next decision time | enable · arm · trade · pause · liquidate |
| Book | held names, entry, unrealised, exit-rule distance | — |
| Pool | effective pool, provenance chips per symbol, source freshness | pin · exclude · un-override · refresh |
| Log | action log, filterable, decision rows expandable | — |

The Pool panel is where "add and remove symbols" lives, and provenance chips make it obvious
*why* each symbol is present — which is the thing a flat list can never show.

---

## 7. Converge with #46 rather than duplicate

Our backtested exit — leave when a position gives back 50% of peak open profit — is a simpler
sibling of #46's adaptive trail. #46 is richer (blowoff detection, off-HoD durability, partial
exits) and was designed from the PENG leak.

Recommendation: implement the give-back rule as one **action in the #50 registry**, so the
systematic strategy and the semi-auto managers draw from the same catalog. Do not build a private
exit path inside the strategy.

---

## 8. Phasing

| phase | delivers | depends on |
|---|---|---|
| **1 · Pool** | schema, effective-pool view, CRUD API, nightly import, Pool panel | — |
| **2 · Lifecycle** | strategy registry, states, `ARMED` dry-run, status API + panel | 1 |
| **3 · Log** | action-log table, decision rows, WS topic, Log panel | 2 |
| **4 · Schedule** | session-aware scheduler, idempotency, refresh-before-decide gate | 1–3 |
| **5 · Trade** | order submission from `TRADING`, risk gates, kill switch | 4 |

Phases 1–3 are observable and safe: the strategy computes and publishes without submitting an order.
That is deliberate — it lets the thing run beside paper trading for weeks and be judged on its
published decisions before it is allowed to act on them.

## 9. Deliberately out of scope for now

Multi-strategy capital sleeves and cross-strategy arbitration (#80) — one strategy first.
Per-trade journaling and setup tags (#110) — the action log covers provenance; the journal is a
human layer on top. Analytics beyond what `kumo-strategies` already computes (#112). Intraday
decisioning — measured identical at 30/60/120/390-minute cadence, so it buys nothing and adds
execution risk.
