# ADR 0001 — Execution ownership & strategy-attribution model

- **Status:** Accepted (2026-07-11)
- **Deciders:** the operator. Reviewed by Perplexity (idiom critique — reversed to this position on the source finding) and codex (source-grounded review of installed `nautilus_trader==1.229.0`).
- **GitHub:** #72 (this ADR) · epic #68 (cockpit data model)
- **Supersedes:** the earlier "single master strategy" lean (Perplexity's original recommendation; both reviewers reversed on the source finding below).

## Context

The cockpit is one `TradingNode` hosting multiple **strategies** (was "lane"): a style bucket + capital
sleeve + management mode — `MANUAL` (discretionary) · `MOMENTUM` (single-name rip/breakout) · `ETF_AUTO`
(rotation). Momentum trading breaks the "position == engagement" assumption: it sells strength and rebuys
weakness, so a name is often **flat but still being worked**. We need:

1. per-strategy attribution of positions/orders/P&L on **one** broker account, and
2. a **cycle** view whose lifetime = open→closed (spanning flats), not the raw position lifetime.

IBKR and Alpaca are both **account-NETTING** brokers.

### The finding that decided this

In `nautilus_trader` 1.229.0 (`execution/engine.pyx`), the NETTING position id is:

```python
PositionId(f"{instrument_id}-{strategy_id}")
```

It is deterministic **per (instrument, strategy)** and **carries the strategy id**. Consequences verified in
source:

- Each strategy natively gets its **own logical net position** per instrument (`AAPL-MANUAL`, `AAPL-MOMENTUM`),
  each with its own realized-P&L lifecycle.
- At flat (`quantity == 0`) a position is **closed** (`model/position.pyx:619`: side → FLAT, `ts_closed` set).
  The next fill **opens a fresh `Position` under the same id**, the prior leg archived on reopen
  (`_reopen_position` → `Position(instrument, fill)`; `_handle_position_update`: closed → `_open_position`).
  Note: durable **state snapshots require `snapshot_positions`/`snapshot_orders` config (default OFF)** — the
  close/reopen lineage above is engine behavior; persisted snapshots are a separate, off-by-default feature
  (see the restart gate below).
- Live reconciliation for NETTING **sums all cached strategy positions** for the instrument/account and
  compares that aggregate to the broker's single account-level position report (which has **no** strategy id).

So **per-strategy attribution and per-leg realized P&L are native**. The only genuinely hand-built part is
threading same-id legs + the flat-gap working orders into one **cycle** with a running total.

## Decision

**Run each strategy as its own Nautilus `Strategy`; add a cross-strategy coordinator. Do NOT use a single
master strategy.**

A master strategy would collapse the native per-strategy position/P&L objects back into app-level bookkeeping
— re-implementing exactly what Nautilus already provides. Per-strategy strategies keep attribution native.

Supporting rules:

1. **Broker net is the only hard reconciliation anchor.** Nautilus verifies the *sum across strategies* vs the
   venue net; **per-strategy splits are unverified by the broker**. Surface the net as the anchor row.
2. **Cross-strategy coordinator** owns risk, capital-sleeve reservation (working orders — ARMED entries +
   protective exits — count toward risk/buying-power), opposing-strategy policy, and order arbitration so
   strategies don't race cancel/modify on the same instrument (#80, #12).
3. **Opposing strategies on one account = attribution overlay only**, not real gross exposure (they net
   economically). A real opposing bet requires a **sub-account**; otherwise block it (coordinator policy).
4. **External/manual broker activity cannot be provably attributed** to a strategy → **claim/quarantine**
   (#79). This must be **bound to Nautilus config**, not left to defaults: set `external_order_claims`,
   `filter_unclaimed_external_orders`, and `generate_missing_orders` explicitly. **No `VENUE`/`RECONCILIATION`
   event may attach to a strategy cycle unless claimed.** Only the coordinator allocates new risk after an
   unclaimed event; unclaimed deltas are quarantined until manually assigned or written to a non-strategy bucket.
5. **Cycle is a projection layer above native positions** (#73): group native positions + snapshots + working
   orders by the canonical identity below; states `HELD`/`ARMED`/`WATCH`/`CLOSED`; **P&L derived from native**
   positions/snapshots/fills — never a parallel ledger.
6. **Identity contracts:**
   - **Canonical identity = `(account_id, client_id, instrument_id, strategy_id, cycle_id)`.** Reconciliation
     is account-scoped and sub-accounts are the escape hatch, so `account_id`/`client_id` are part of identity —
     not just `(instrument, strategy)`. These fields are **required on engine DTOs and orders**.
   - Stamp `strategy_id` (native) + explicit **durable `cycle_id`** + `manager_id` from day one — even MANUAL.
     Today only `strategy_id` exists on the DTOs/orders; **`cycle_id` and a nullable `manager_id` MUST be added
     in round 1, before any UI consumer**. The native position id is a *strategy* key (reused across cycles, no
     flat-armed coverage), so it is **not** a cycle identity.
   - **Do not** reuse Nautilus `TradeId` as the domain trade/cycle id — it is a fill id.
7. **Projection runs in the engine and emits an engine-authored `TradeCycleDTO`** (Phase 2). The UI/API **must
   not** derive cycles from `PositionDTO` or the `ui:stream` (display-only, droppable). The current
   position-based surface (`PositionDTO`/`PositionsResponse`) is superseded by the managed-book DTOs, not
   extended.
8. **Restart durability is a hard Phase-1/2 gate.** Nautilus state snapshots default OFF and the node does not
   yet enable exec-engine snapshot/cache persistence — so "rebuild from snapshots" is NOT free. Before cycle
   projection is considered restart-safe, EITHER durable Nautilus cache/position/order snapshot persistence is
   enabled, OR an explicitly equivalent broker-report source (#75) is in place. Rebuild logic must be identical
   live vs restart (#74).
9. **Per-order strategy ownership (fill-attribution principle).** Each broker order is owned by exactly ONE
   strategy **at creation**; the coordinator splits a desired size into per-strategy **child orders** — no order
   is shared across strategies. Fill attribution is then trivial (fill → owning order → strategy); post-hoc
   allocation is *reconstruction only* (FIFO by order id/timestamp when a venue aggregates fills). This
   sidesteps the same-direction partial-fill allocation problem entirely. (Perplexity review.)
10. **`cycle_id` is first-class ENGINE state, not a UI concept.** Assigned deterministically at the **first
    economically-significant open** (first fill / confirmed working-order commitment), **immutable** until fully
    flattened + settled, and **reconstructable from raw events** in the same order live vs replay. A cycle_id that
    depended on async UI/external state would make replays disagree with runtime attribution. (#73/#74.)

## Consequences

- **Positive:** minimal hand-rolled accounting (attribution + per-leg P&L native); each strategy has a
  first-class net position + P&L lifecycle; round-1 shrinks to a thin cycle-threading layer (#73).
- **Negative / owned risk:**
  - The architecture depends on an **internal Nautilus source invariant** (`{instrument}-{strategy}` id,
    snapshot-on-reopen, sum reconciliation). Pin `nautilus_trader==1.229.0` and guard it with replay tests
    (#76) so a version bump can't silently break attribution.
  - Per-strategy splits are **not broker-verifiable**; attribution drift after external/manual activity is a
    policy problem (quarantine), not something reconciliation can prove. Only sub-accounts fully close it.
  - **Blocker:** Alpaca `generate_fill_reports` returns empty today — cycle P&L + restart can't be trusted
    until fill/order-report completeness lands (#75).

## Alternatives considered

- **Single master strategy** (Perplexity's original): simpler single-controller ownership, but discards the
  native per-strategy position/P&L objects and forces attribution into app metadata. Rejected as the default;
  reconsider only if strategies become mere modes of one alpha process.
- **Sub-accounts per strategy:** the only model that makes per-strategy exposure *broker-verifiable* and
  supports real opposing bets. Not required for round-1 (single MANUAL); kept as the escape hatch for genuine
  opposing strategies.

## Round-1 impact

Small **but not zero**. Ship single-strategy MANUAL now with one MANUAL Nautilus strategy. Today the DTOs/orders
carry only `strategy_id`; round 1 **must add** durable `cycle_id` + nullable `manager_id` (MANUAL has no
manager) **before any UI consumer**, so the coordinator + multi-strategy work (Phase 5) is not a rewrite. The
restart-durability gate (Decision §8) and broker fill-reports (#75) are also round-1 obligations, not later.

**Reconciliation note (all phases):** derived cycle P&L is a **projection**; **broker P&L is authoritative**.
Fees (incl. pass-through ECN), corporate actions, FX, and slippage make them differ — track the difference as an
explicit reconciliation item, never hide it. Canonical MTM price source per instrument must be **documented**
(Alpaca IEX vs SIP vs execution price diverge).

## Deferred — Phase-5 production-hardening obligations (from the Perplexity + codex reviews)

These do NOT block round-1 (single MANUAL) but MUST be modelled before a second strategy trades live — captured
here so they aren't rediscovered as production surprises:

- **Account-risk model as its own subsystem** (#80). Buying power, margin, **PDT day-trade count, locate/short
  availability are ACCOUNT-level and shared** — capital sleeves are NOT fixed carve-outs (they vary with MTM;
  the broker can shrink all sleeves at once). Coordinator decisions must run off a single **broker-truth
  AccountState snapshot** with atomic reservation; reservations must **reconcile from broker ground truth** on
  every order state change (reject/expire/partial), not incremental mutation. PDT trip = an **account-level
  event** that restricts all strategies, not attributable to one.
- **Coordinator as a first-class engine component** (#80): non-blocking, **bounded-latency** arbitration
  (priority-based, no deadlocks); **fail-closed** policy + health/circuit-breaker (it's a single point of
  failure); separate **fast path** (pre-trade checks) from **slow path** (projection/reporting). If it lives in
  Python/Redis off the Nautilus event loop, that undermines the native RiskEngine — evaluate co-location.
- **Canonical working-order ownership** for cancel/modify (one owner; others request via owner) — avoids
  double-cancel / ghost-order races.
- **Corporate-actions handler**: splits/dividends change quantities/basis at the account level → re-allocate
  per strategy (split ratio) or explicitly exclude from strategy P&L; never treat as a bare price jump.
- **Alpaca specifics**: validate attribution on **live, not paper** (paper fill behavior differs); **fractional
  shares** → high-precision decimal + an explicit **residual/dust** bucket for net-vs-sum mismatches.
- **Sub-account namespacing** (codex caveat): the native NETTING position id is only `{instrument}-{strategy}`
  (no `account_id`). Before sub-account routing (the real-opposing-bet escape hatch), require account/client
  namespacing of `strategy_id` or separate node/cache ownership per account.
