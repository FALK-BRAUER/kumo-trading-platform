# Build spec — the stable concept, ready to build

> Status: **READY TO BUILD.** 2026-08-19.
> Everything here survived four independent reviews unchanged, needs no architecture decision, and is
> reversible on its own. The seam redesign (ADR 0002) is **out of scope** and stays parked — it has
> changed shape in every review round, and that is a signal about its readiness, not about its value.

## The concept, as build rules

Five invariants. These are the stable part — reviewers have attacked the designs built on them and never
the laws themselves.

1. **One writer per fact.** No component keeps a second copy of something another component owns.
   A fact with two writers will drift, and every drift so far has produced a wrong number in production.
2. **Intent is declarative and durable.** Store what should be true, not the message meant to achieve it.
3. **One controller per subject.** Two loops over one resource oscillate. Independence of *fate* is a
   safety property; independence of *opinion* is the bug.
4. **Every action reaches an observed outcome, or it is still in flight.** In flight, terminal, or
   unknown — no fourth state. "Accepted" is not an outcome; elapsed time is not an outcome.
5. **Contracts are executable and checked where failure is cheap** — at construction, at a watched
   deploy, not at an unwatched open.

And one rule the reviews produced that governs all seven items below:

> **A mechanism that has never executed is not a safety argument.** Four of these seven items exist
> because something was built, configured, deployed, and never once ran. Each therefore ships with a
> test seen failing first — an unfailed test is how this category is created.

## Build order

Ordered by dependency, then by live exposure. Each item is independently shippable and independently
revertible.

---

### 1 · `mark_to_market` has no caller — the wind-down gates on a stale number

**Owner:** cockpit · **Size:** small · **Evidence:** `budget_store.py:175`; one occurrence outside its own
definition, the `__all__` entry.

Its own docstring: *"`actual` is defined as net asset value, so P&L must reach it or a profitable sleeve
would show the same number forever."* Nothing calls it. `actual` moves only on transfers.

This is now critical rather than cosmetic: with the paced reducer deleted, **`budget_gate` is the entire
wind-down**, and `is_reducing` — the flag refusing every entry — is computed from `actual`.

> **BLOCKED — do not build as specified.** kumo-trading-strategies' fourth reviewer found a deadlock in this
> item before it shipped. A tick that reads broker *positions* supplies only the market-value half of
> `actual`; `budget.py` defines it as **cash plus market value**, and there is no per-sleeve cash ledger
> anywhere. So a sleeve that goes fully flat gets `actual = 0` permanently, and
> `deployable = max(0, min(actual, target) - deployed)` is then **0 forever — it can never buy again.**
> BCTROT-004 is in exactly that state right now (`actual 0`, `target 20,000`, deployable 0), and this
> item would extend the same trap to MOMENTUM-002 at the end of its own wind-down. It would also create
> a second writer with semantics incompatible with `on_sell_fill`'s decrement-by-delta.
>
> **And my stated acceptance criterion did not catch any of it** — *"`actual` moves without a transfer"*
> passes while every sentence above is true. That is an undiscriminating acceptance test, which is the
> same defect class the spec is written to eliminate.
>
> **Second design input, found 2026-08-19 (kumo-trading-strategies):** the ledger must distinguish a fill that
> moved REAL capital from a fill that only corrected a local/venue disagreement.
>
> Concrete case. On the 08-18 restart, reconciliation found AEM cached at 18 shares against a venue
> reporting none, and closed it. `on_sell_fill` cannot tell a synthetic flatting order from a real sale,
> so it decremented the sleeve by ~$3,240 — visible in `sleeve_transfer` as
> `MOMENTUM-002 -> UNALLOCATED 3,251.70`.
>
> That moved `actual` **further from** truth, not toward it. Those 18 shares were never real, and `actual`
> is hand-seeded rather than derived from live positions — so it was never inflated by AEM in the first
> place. Subtracting a sale of something that never existed subtracts real, non-phantom dollars for a
> non-event.
>
> **A synthetic flattening order should reconcile POSITIONS, not move CAPITAL.**
>
> > **What it needs first:** a NAV source that includes sleeve cash. That does not exist and is a design
> question, not a wiring one. The two failing tests written for this item stand and stay red until it is
> answered — they are correct about the missing caller; the fix is what was wrong.

- ~~**Change:** call `mark_to_market` on the existing periodic tick that already reads broker positions.~~
- ~~**Acceptance:** `actual` moves without a transfer.~~

### 2 · `allocated_equity` has no producer — sizing runs off the whole account

**Owner:** cockpit · **Size:** one assignment · **Evidence:** zero occurrences in cockpit; both gateways
construct bare `RiskLimits()`.

`pgrunner.py:801`: `equity = self.limits.allocated_equity or self.broker.equity()`. It is always the
second branch, so MOMENTUM sizes ~$10k a name off ~$100k against a $20k target built for eight names.
**This is the cause of the 67k overflow and of item 1's `is_reducing`,** and it will do the same to any
strategy that is ever funded.

- ~~**Change:** populate `RiskLimits(allocated_equity=...)` at gateway **construction**.~~
  **CORRECTED — that is the stale-copy design.** The target is a number the operator edits while the node
  runs (QC345's was cut 40k→20k mid-flight on 2026-08-15), so a construction-time capture is a second
  copy of a fact settings owns: the Law 1 defect arriving inside the fix for a Law 1 violation. **Derive
  it PER SESSION**, on the path `SessionGateway` already rebuilds for exactly this class of reason.
- **Change:** `_limits_for_session()` reads the target live and `replace()`s it onto the limits handed to
  the runner. **Guard the `replace`** — `RiskLimits` may predate the field, and an unguarded call raises
  `TypeError` before any journal row, which is the `slot` defect rebuilt.
- **Failing test first:** bind the REAL `RiskLimits`, not a local stand-in. A double carrying a field
  production lacks is how the guard above shipped missing.
- **Acceptance:** a 20k sleeve sizes ~$2,000 a name, not ~$10,000, and the dependency is pinned to a
  revision that has the field. Expect ~8% of budget unspent to share-flooring on higher-priced names —
  arithmetic, not a defect, but visible in position sizes.
- **NOT covered:** QC345 sizes off the account by a different route (`_equity_per_position`) and is
  unaffected by this change.

### 3 · Outcome feedback — a rejected exit is recorded as a success

**Owner:** kumo-trading-strategies (ks#51) · **Evidence:** `exec_action_log` 2026-08-14,
`SELL 88 VCTR … {"ok": true}`; VCTR still held, 88 shares.

`submit()` returns ok while the order is in flight; the consumer treats it as terminal and never retries.
Correct under **any** boundary — this does not wait on the seam.

- **Change:** read the order's terminal state rather than the submit's return; journal the terminal
  outcome.
- **Failing test first:** a venue-rejected sell leaves a journal row saying it succeeded.
- **Acceptance:** rejection produces a terminal row; the position's claim survives for the next session's
  re-evaluation.

### 4 · Price staleness — both guards are dead

**Owner:** both · **Evidence:** `last_price()` reads no timestamp anywhere; the entry guard has fired 8
times ever, all before the commit that introduced a silent stale-bar fallback; the exit guard has fired 0
times in 1013 rows.

A prerequisite for anything that actuates automatically: a stale price sizes an entry or fires an exit
with nothing detecting it.

- **Change:** carry a timestamp with the price and refuse to size or exit on a price older than a bound.
- **Failing test first:** feed a price stamped an hour old; watch it be used.
- **Acceptance:** the guard fires on the stale fixture, and the fixture is proven able to violate it.
- **Status:** the *capability* is built (opt-in `max_age_ns`, both repos). **No caller has opted in yet**,
  so nothing is enforced. Fail direction, decided: **fail toward whichever default is cheaper to undo** —
  a missed entry costs one session and is recoverable, so sizing REFUSES on stale data; an absent stop
  costs an uncovered window with unbounded downside, so protection ACTS on stale data and logs.

### 5 · Session-failure alerting — a dead week found by a human on day three

**Owner:** cockpit (#349, #199) · **Evidence:** the `slot` TypeError was journalled correctly on 08-17 and
08-18. Nothing read it.

Note `build_session_observer` **is** wired (`momentum.py:371`) — it fires on a *result*, and a session
that raises never produces one. So the gap is the failure path, not the hook.

- **Change:** `GET /strategies` carries `last_successful_session` and `sessions_since_success`; a strategy
  past its last scheduled slot without an outcome raises through the existing Telegram transport.
- **Failing test first:** two consecutive failed sessions produce no alert and no API signal.
- **Acceptance:** the alert fires on the second miss, and states which slot.

### 6 · `_submit` never passes `position_id`; coverage ignores reservations

**Owner:** cockpit · **Evidence:** `engine_node.py:3150` calls `submit_order(order)`;
`risk/engine.pyx:425` gates the whole reduce-only check behind `command.position_id is not None`; the
Alpaca exec client has zero occurrences of `reduce_only`. And `unprotected_positions`
(`protection.py:353`) uses `protective_quantity` alone while `reserved_quantity()` sits at `:247`, already
used at `:525`.

- **Change:** pass `position_id`; subtract `reserved_quantity` in the coverage audit.
- **Failing test first:** a reduce-only order that would increase a position is accepted; and a stop is
  planned for shares a live exit already reserves.
- **Acceptance:** both refused, both with a reason.

### 7 · `TRANSFER_TO` is empty — freed capital cannot reach BCTROT

**Owner:** operator decision, then cockpit · **Evidence:** `TRANSFER_TO = ''`, so the one existing
`sleeve_transfer` row reads *"donor is over target; no recipient with headroom"* and the capital went to
UNALLOCATED.

The settings text promises BCTROT *"takes over gradually as MOMENTUM sheds positions"*. It cannot while
this is blank.

- **Change:** set it. Then BCTROT needs a lifecycle row to trade at all (#320), which is a separate
  decision.
- **Acceptance:** a MOMENTUM sell moves capital to BCTROT's sleeve rather than UNALLOCATED.

---

## Explicitly out of scope

The decision/actuation seam, the membership vocabulary, the ExecAlgorithm, the shadow comparison, the
cutover. All of it stays in ADR 0002, parked. **Nothing in this spec commits to any of it**, and items 1,
2, 4 and 6 are prerequisites for it in any case — a platform that actuates automatically must not be
built on a stale NAV, an unset allocation, dead staleness guards, or unenforced reduce-only.

## Definition of done, per item

1. A test that was **seen failing** before the fix, and named in the PR.
2. The fix.
3. Evidence the mechanism actually executed in production — because four of these seven were built,
   deployed, and never ran. A green test is not that evidence.

---

# Item 8 — the exit reservation defect. AGREED BOTH SIDES, READY TO BUILD

> Added 2026-08-19 after it stopped the rotation live, in the session this deploy was made for.
> Scoped and agreed with kumo-trading-strategies; both sides verified the diagnosis independently.

## What happened

MOMENTUM-002 decided at 13:35:00Z — the first decision since 08-14, so the `slot` fix worked. Then:

```
decided   hold 8 · enter 3 · exit 2
exits     FSM 933 REJECTED · VCTR 88 REJECTED   "insufficient qty available (0)"
entries   AEM filled 10sh · AMGN, LMT skipped (position cap — correct: failed exits freed no slots)
```

**The shares were never missing.** `long_market_value 75,000.00` from the broker, FSM 933 and VCTR 88
both held. `available: 0` means *unreserved* is zero — both positions are fully reserved by their own
protective stops:

```
PROT-SELL-FSM-XNYS-c1818b3e    ACCEPTED  SELL 933  leaves_qty 933
PROT-SELL-VCTR-XNAS-fb20d169   ACCEPTED  SELL 88   leaves_qty 88
```

A full-quantity sell cannot be placed alongside a full-quantity resting stop. **The VCTR stop has now
blocked that same exit twice, five sessions apart** — 08-14 and today. Third live reproduction in six days
(VCTR 08-14, AEM 08-18, FSM+VCTR 08-19).

## Why neither repo can fix it alone

- **`pgrunner` has no cancel logic anywhere**, and no visibility into a stop it did not place, resting
  under a `strategy_id` it does not own.
- **The stop is cockpit's**, on a 60-second reconciler that re-places it within ~53s of any cancel
  (measured on CGAU).

Cancel from the strategy side and the reconciler re-places it. Suppress from the platform side and the
exit does not know to retry. It has to be one change.

## The agreed shape

**(a) The platform owns it.** Only the platform can coordinate a cancel against its own reconciler's
re-place race; the strategy layer structurally cannot.

**(b) Only the exit-side SELL path changes.** Entries have no stop conflict — `BUY` via `broker.submit()`
is untouched. Exits get a new call shape (`broker.exit(symbol, qty)`, or an `intent="exit"` flag the
broker branches on) which internally does:

```
1. assert an exit intent  → suppresses protection for THIS position, TTL-bounded,
                            expiring, failing toward protecting
2. cancel the specific PROT-SELL-* for that instrument
3. await the RESERVATION clearing — `_await_shares_available`, which polls the venue's
   available quantity and already does this correctly. NOT order status: Alpaca frees
   shares on the confirmed cancel, and those are not the same event
4. submit the sell
5. let protection re-arm on its next tick
```

`pgrunner`'s job shrinks to *declaring exit intent*. Nothing else changes on the strategy side.

**(c) Narrow and additive.** One new interface point on the broker. **No seam move, no ADR 0002.**

## Already solved, connects for free

The retry must vary the client order id per attempt or Nautilus denies it locally
(`trading/strategy.pyx:865-868`) — the defect kumo-trading-strategies found in their own `#51`. Their
attempt-counting reads terminal rejections per symbol regardless of *why* it was rejected, so once this
path exists a retry through it gets a fresh id automatically. No extra work.

## Why this is first

Today's deploy shipped sizing, journal identity, price observation time, reduce-only and liveness. All
correct, all verified, all mattered — and **none of them was the thing that stops a rotation.** This is.
