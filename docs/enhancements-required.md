# Enhancements required — 2026-08-19

Assembled from the 2026-08-18 incident work, the ADR 0002 design negotiation with kumo-trading-strategies, and
three independent reviews. Ordered by urgency, not by size. Owner is `cockpit` or `strategies`.

Nothing here is speculative: every item cites evidence from this system's logs, journal, Postgres or
source.

---

## A. Live exposure — these bite before the architecture does

| # | Enhancement | Owner | Evidence |
|---|---|---|---|
| A1 | **Outcome feedback: read the order state machine instead of recording `ok=true` at `INITIALIZED`.** | strategies | VCTR is held **right now**, 88 shares, under a journal row saying the exit succeeded. Correct under any boundary — ships regardless of ADR 0002. (ks#51) |
| A2 | **Price staleness guards — both are dead.** `last_price()` reads no timestamp anywhere; the entry guard has fired 8 times ever, all before the commit that added a silent stale-bar fallback; the exit guard has fired 0 times in 1013 rows. | both | A stale price sizes an entry or fires an exit with nothing detecting it |
| A3 | **Data-outage observability.** The 27-minute Alpaca gap on 2026-08-18 is unfalsifiable from the system's own records — no container survived it, and the one durable signal reporting "ok" through the window monitors a symbol-list CSV, not the price feed. | cockpit | A feed outage must leave a durable trace |
| A4 | **Session-failure alerting.** The `slot` TypeError was journalled correctly on 08-17 and 08-18 and found by a human on day three. `session_alerts.py` exists; the Telegram transport is built and called by nothing. | cockpit | #349, #199 |
| A5 | **`_submit` must pass `position_id`.** `engine_node.py:3150` calls `submit_order(order)`; `risk/engine.pyx:425` gates the whole reduce-only check behind `command.position_id is not None`, and the Alpaca exec client has zero occurrences of `reduce_only`. | cockpit | Reduce-only is unenforced on **every order cockpit sends** |
| A6 | **Protection coverage must subtract reservations.** `unprotected_positions` (`protection.py:353`) uses `protective_quantity` alone; `reserved_quantity()` exists at `:247` and is already used at `:525`. | cockpit | Plans a stop for shares a live exit already holds, then eats `available: 0` |
| A8 | **The backtest/live parity gate has not run since 2026-07-29.** `run-tests.sh` step [2/4] collects two modules that each build a module-scoped `BacktestEngine`; a second engine per interpreter aborts natively, and `set -euo pipefail` fails the whole script. | cockpit | #184. *"A parity gate that never runs is worse than no gate"* — it pins the Nautilus invariants the projection depends on |
| A9 | **The deploy chain can deploy an unmerged PR.** A `gh pr merge` hit a GitHub 503, did not merge, and the `&&` chain deployed anyway — the engine was rebuilt on the same broken code. Related: `compose --force-recreate` reuses the old image ID, and image creation time is not evidence of which commit is running. | cockpit | #329, J-class. Verify the merge landed and the running image ID matches the freshly built one |
| A7 | **Retire stale position claims.** AEM reads qty 2 in `exec_position_state` and 0 at the venue; the reconcile that retires claims has not run since 2026-08-14 because sessions were dead. | strategies | A Law 1 violation standing in production for five days |

## B. ~~Blockers on the ADR 0002 decision~~ — RESOLVED IN THE ADR, retained for the evidence

> All B-items are marked RESOLVED in ADR 0002. They are kept here because the evidence rows are still the
> clearest statement of each problem. **The remaining blockers are listed in the ADR's own status table,
> not here.**

| # | Question | Owner | Why it blocks |
|---|---|---|---|
| B1 | **What does `target_weight` mean for a name the strategy has no opinion about?** `pgrunner.py:533-546` deliberately HOLDS a name with no bar rather than exiting it. A weight vector has no encoding for "absent" — missing-key-means-zero liquidates the book on a feed hiccup. | both | The primitive is incoherent without an answer |
| B2 | **Convergence vs entry-only sizing.** `pgrunner.py:812-815`: sizing is *"at ENTRY ONLY and never rebalanced … measuring anything else would be measuring a strategy we cannot run."* `target_weight` as desired state **means** convergence. Both independent reviewers reached this separately. | strategies | The primitive's semantics contradict why the strategy is profitable |
| B3 | **Sidestep overloads weight 0.** `sidestep.py`: *"the name is sold, the slot stays reserved, and the position is restored on the reversal."* A sidestep-zero must not close the cycle, release the claim or free capital; an exit-zero must do all three. | both | Same value, opposite required behaviour |
| B4 | **What is the durable intent store?** `ExecAlgorithm` has **no `order_factory`**; it can only spawn from a primary a `Strategy` submits, so the strategy computes a delta with a side and quantity. Routing happens once at `submit_order`, `_reset` clears spawn state, and nothing re-delivers a primary after restart. | cockpit | The chosen transport is not the store the ADR describes |
| B5 | **What must BCTROT-004 become before "BCTROT-004 first" means anything?** `actual 0.00`, no lifecycle row (#320), no path to capital (`TRANSFER_TO` empty, #350), zero rows of any kind ever. | cockpit | The migration's first two stages currently exercise **no actuation at all** |
| B7 | **The migration has ZERO working verification mechanisms.** SHADOW is not a parallel observation mode — it is `TRADING → SHADOW`, labelled *'pause': keep positions, stop acting*, and `may_submit_entries` is True only for TRADING. `exec_strategy_state` holds **one row per strategy** (verified: a single row, `MOMENTUM-002 | TRADING`). A strategy is SHADOW **or** TRADING. A shadow twin cannot be registered either — `order_id_tag` must be unique node-wide, and `external_order_claims` are exclusive and collide at `add_strategy`. | both | So the decision half has no verification mechanism, and the actuation half's (BCTROT-first) proves nothing per B5. Both repos checked these mechanisms' *existence* and never their *exclusivity* |
| B6 | **What is the rollback?** Once the old path is deleted, `git revert` + redeploy is the only route — and `deploy/Dockerfile.backend:35-39` installs kumo-trading-strategies from a local checkout, so reverting one repo alone recreates the version-skew class that produced #346. | both | "No fallback" is a design rule; it is not "no rollback" |

## C. Platform enhancements — the work itself

| # | Enhancement | Owner | Reference |
|---|---|---|---|
| C1 | Executable contract: `SessionRunner` Protocol + `assert_conforms`, run in **both** CIs and called at cockpit's node construction so a bad wiring refuses to boot | strategies defines, cockpit calls | ks#45 |
| C2 | Contract suite driving the **real** wiring — backtest never touches the injected-runner path, so nothing today asserts the two repos agree | strategies | ks#50 |
| C3 | Manager framework: per-row `next_attempt_at`, attempt accounting with a classified `last_reason`, and a real `DEFERRED` state instead of overloading `ARMED` | cockpit | #245's own review, never built |
| C4 | Urgency as **control flow**, not a tag — `_forced_exits` bypasses the already-decided gate, the slot idempotency **and** the bar-coverage floor. Three bypasses | cockpit | ks#49 |
| C5 | Session outcome record for a **null decision** — an order-shaped log cannot express "considered, declined", which is the ambiguity that hid 08-17 and 08-18 | both | ADR open item |
| C6 | Lifecycle API — nothing can move a strategy to TRADING; the only existing row was inserted by hand | cockpit | #320 |
| C7 | Wind-down actually reaching BCTROT: `TRANSFER_TO` is empty so freed capital returns to UNALLOCATED, and nothing sells toward target | both | #350, ks#46 |
| C8 | `stop_reenter_rearm` — **zero rows of any state, ever**. The half that re-enters has never been instantiated. `pyramid_watch` likewise | cockpit | #257 |
| C9 | Convergence-loop discipline: every declarative desired state names its converger, its clock, and what its silence looks like | both | The system has exactly one declarative desired state today and has never converged on it |
| C10 | Foreign venue state: resting exit orders this system did not place. Unrelated to node registration, and not covered by the #348 freeze reasoning | cockpit | Review finding |

## D. Frozen — do not fix, these are specifications for the replacement

`_reserve_attach_command`'s scoping guard · `deferred_flatten` repairs · the hand-rolled cancel-then-wait
in `_handle_flatten_command` · `_await_shares_available` timeout tuning (measure once, apply in the new
place) · the second gateway's divergence · PEAK's direct cancel/submit paths (its trigger logic, chain
state and leash survive and may be fixed).

The rule: **if a component is on a deletion list, the only legitimate work on it is understanding it well
enough to reproduce its behaviour. Its bugs are specifications, not tickets.**

---

## Order

**A1 → A5, A6 → A2, A3, A4 → A7** — none depend on the ADR decision, and A1 is the prerequisite for
everything in C.

**B1–B6 must be answered before ADR 0002 can be Accepted.** B2 is the one that may invalidate the
primitive outright.

**C follows the decision.** C1 and C2 are correct under either boundary and can start immediately.


---

## E. The frame was too small — an independent catalogue found 95 incidents, not twelve

A reviewer built its own incident list from 200 issues, 445 commits, engine logs, 8 Postgres tables and
the handoff documents, **without reading ADR 0002 or the plans**. It derived its own taxonomy from the
data. The result reframes the work:

> *"Almost nothing here is a wrong algorithm. Nearly every incident is a **failure of correspondence** —
> between two records, between a component and its neighbour, between what a thing reported and what it
> did, between the artifact and its source."*

Ten categories, 85 incidents, plus 10 outliers that fit none. The two the ADR was built on are real but
are two of ten. The categories that matter most for this list:

| Cat | Shape | n | Status |
|---|---|---|---|
| **B** | **Built, configured, deployed, never executed** | **14** | **Never had a fix campaign** |
| E | One side of a contract moved | 12 | Partly covered (C1, C2) |
| I | The verification apparatus certified nothing | 10 | Partly covered (C2) |
| A | Two ledgers for one fact, wrong one authoritative | 9 | Law 1 |
| D | Teardown window — protection removed before its replacement existed | 8 | Laws 2/3 |
| H | A failure with no addressee | 8 | A4 |
| C | Loop never closes — success recorded before an outcome | 7 | A1 |
| F | A guard that blocks recovery rather than preventing the bad state | 6 | **Uncovered** |
| G | A constant that assumed a scale it did not have | 6 | **Uncovered** |
| J | The running artifact could not identify itself | 5 | Partly (#329) |

**Category B is the finding.** Fourteen features built, configured, deployed and never once executed —
including a measured **≈$1,463** loss — the validated give-back exit was inert because every trail was
seeded `peak == entry`; **this one is historical**, fixed by kumo-trading-strategies `689efd0` on 2026-08-10,
after the window that measured it, and the cost was never tallied until now — and **≈6 points of
return** from market-on-open, which is genuinely open: grepping the whole strategies source for
`AT_THE_OPEN`/`market_on_open`/`MOO` returns exactly one hit, `backtesting/runner_cadence.py`, with
zero reach into any live adapter,
PYRAMID armed zero times since it was built, trade receipts structurally unable to fire, and an alert
transport wired to nothing. Its own summary of why it accumulates:

> *"These are invisible by construction: nothing errors, nothing logs, every test passes. Category A gets
> tickets because it produces a visibly wrong number; B produces **nothing**, which is why it accumulates."*

**Two uncovered categories to add to the work:**

| # | Enhancement | Owner |
|---|---|---|
| C11 | **Inertness detection** — a shipped mechanism that has never executed is a defect, not a quiet feature. Every gate, manager kind and scheduled path needs a "has this ever fired" signal, surfaced. This is category B's fix campaign and it has never had one | cockpit |
| C12 | **Guards must fail toward recovery, not away from it.** #306's own words: *"A guard that cannot stop the bad thing but blocks recovery from it is strictly worse than no guard."* Category F is six instances of a guard that only blocked the fix | cockpit |
| C13 | **Scale-free constants.** PEAK's 2.5% trail against FIG's 7.94% ATR is 0.31× ATR on one name and 1.52× on another; `give_back_frac` has the same shape. Widths must be volatility-normalised | cockpit |


---

## F. Surfaced by the independent reviews, not previously tracked

| # | Enhancement | Owner | Evidence |
|---|---|---|---|
| F1 | **An IN-STRATEGY shadow branch is the verification mechanism** — a second code path inside the *same running strategy*, computing both decisions, logging both, submitting one. It sidesteps all three exclusivity walls in B7 because it is not a second Nautilus registration at all: no lifecycle row, no `order_id_tag`, no `external_order_claims`. Its risk is narrow and directly testable — assert the shadow branch never reaches `submit_order`, via a broker stub that raises if it does | strategies | Fixes B7 |
| F1b | ~~Offline replay of one panel through both decision paths~~ **NOT AVAILABLE — depends on something that does not exist.** `exec_pool_refresh` holds exactly **2 rows**, latest-per-source, overwritten in place (verified). There is no stored historical panel to replay against. It becomes a complementary check *after* panel-history durability is built, and is not a substitute for F1 in the meantime | both | Panel history is its own work item, F1c |
| F1c | **Panel history durability.** Nothing in this system can reconstruct the inputs a past decision was made from. That blocks offline replay, blocks any after-the-fact "why did it decide that", and blocks a whole class of regression test | both | `exec_pool_refresh` is latest-per-source only |
| F2 | **The 2026-08-18 UI publish outage was never diagnosed and no issue was ever filed.** `ui:stream` stopped at 16:33:37Z with the engine alive throughout and all strategies READY. A rollback to a byte-identical backend **also did not publish** — so the evidence excludes the code and points at container-instance state that was never identified. It is resolved now, with nothing recording why | cockpit | The only incident in the record with a deliberate exclusion of source as cause |
| F3 | **~$1,074 of all-time realized P&L is unattributed, and the completeness flag is structurally blind to it.** `realized_periods.all` reports +$138.50 with `unmatched: 0` while equity implies −$935.63. Dropping a contiguous run of history removes both legs of its round trips, so nothing is left looking unmatched | cockpit | #345 §1. Two confounds unexcluded: paper dividend credits, and EXTERNAL→MANUAL transfers with CARRY_OVER pricing |
| F4 | **The symbol pool carries strings that are not tickers**, and silently drops them from ranking every session. `{"unreachable": ["BLLLN","GTLAB","IQVIA","JEPO"]}` — `IQVIA` is a company name; the real ticker `IQV` ranked 13th in the same session. Two long-standing red tests specify the opposite behaviour and have been red long enough to read as background | cockpit | #267. The journal says "until it restarts"; the engine *had* restarted hours earlier — a restart does not fix a typo |
| F5 | **#313 was reverted with its question explicitly open, and the issue is CLOSED.** A change making an Alpaca `held` stop leg count as *not* protection turned out to be a false alarm on a native bracket, and the revert states the underlying question is still unanswered: across 14 bracket stop legs since 2026-07-01, **not one has ever filled**. The probe it specified was never run | cockpit | Codified rule: measure venue behaviour, do not infer it |
| F6 | **A restart silently consumes a decision window.** The upstream fix now logs it (`STARTED AFTER 1 of today's decision slots — did not run and will NOT be run late`), but nothing alerts on it, and the same thing happened silently on 2026-08-17 when a deploy landed after 09:35 | both | Pairs with A4 |
| F7 | **Quiet hours silenced the entire trading session** — a correctly built, correctly configured feature whose specification was wrong for an operator in SGT trading US hours. Removed; the stale docstring survives at `strategies/session_alerts.py:5` | cockpit | The inverse of the built-never-executed class: it ran flawlessly and suppressed everything that mattered |

### Scope note

ADR 0002's own summary of what gets built — *"one ExecAlgorithm; `position_id` on submit; reserved-aware
coverage"* — **undercounts by three to five components.** Not in it: the TTL-bound suppression state and
its renewal loop, the enforcer's read-the-authority rework, kumo-trading-strategies' new *"claimed, externally
managed"* reconcile state, and the two prerequisites above (a workable verification mechanism, a fundable
BCTROT-004). That summary is what a reader estimates effort from.

### On the claim this list replaces

ADR 0002 says *"twelve incidents, both shapes, no residue."* The twelve are never enumerated anywhere,
and an independent catalogue found **95**. A claim of exhaustiveness that cannot be checked was doing the
work of a proof.


---

## G. Found 2026-08-19 — deterministic order ids make every retry path inert

| # | Enhancement | Owner | Evidence |
|---|---|---|---|
| G1 | **Any retry that resubmits must vary the client order id per attempt.** Nautilus denies a duplicate id *locally* (`strategy.pyx:865-868`) and the denial journals identically to a venue rejection, so a retry loop records itself as "tried, refused" without ever reaching the venue | both | Found by kumo-trading-strategies' review of their own `#51`; verified in the Nautilus source. It also invalidates ADR 0002's entire retry section, which four reviews read without catching |
| G2 | **`_protection_coid` and `FL-{cid[:20]}` are deterministic**, so cockpit's retry surfaces carry the same defect | cockpit | #295: a retry on the same coid produced two terminal events on one order, and `load_orders` then killed `TradingNode` construction on **every** subsequent boot until Redis was cleared by hand |
| G3 | **A double that cannot reproduce production's failure is not a fixture.** Six instances tonight across both repos; this one hid a defect in the fix for the defect that started the night | both | Standing rule: bind the real thing to a narrower host, or make the double reject what production rejects |
