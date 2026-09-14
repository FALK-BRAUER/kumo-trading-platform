# ADR 0002 — The decision seam: strategies decide, the platform actuates

- **Status:** **AGREEMENT WITHDRAWN, 2026-08-19.** A third independent review disproved four of the twelve
  resolutions against source and live data, and found a hole in the membership fix itself. Do not
  implement. Nothing is being built.

> ### The lead finding: the membership set has no meaning for silence, absence or staleness
>
> The seam's entire content is a declared set per `(strategy, session, slot)`, and the platform
> *converges* on it. The document never states what the platform does with (1) a symbol in **none** of the
> five states, (2) **no set published at all** for a session, or (3) **how long** a published set stays
> authoritative.
>
> That is not hypothetical. **The strategy is silent on 4 of the last 11 sessions** — and it is silent
> *deliberately*: `pgrunner.py:498-507` (thin ranking), `:466-479` (bar coverage), `:250-256` (stale
> sources), `:283` (daily-loss halt), with `engine.py:355-366` stating the reason outright — *"An EMPTY
> ranking is not a decision to sell the book."*
>
> Today silence is safe because the strategy is the actuator: publishing nothing does nothing. Under this
> ADR the platform actuates, and **both readings of silence are catastrophic**:
>
> - **absent ⇒ EXIT** reproduces exactly the liquidation those guards exist to prevent, one layer up where
>   they no longer apply;
> - **last set stands** means the platform would have kept converging on the 2026-08-14 set through 08-15,
>   08-17, 08-18 and 08-19 — including *entering* names ranked five days earlier — while the strategy was
>   dead with a `TypeError`.
>
> The document already contains the fix and applies it in exactly one place: it derives a TTL for the
> protection-suppression authority three separate ways and concludes that an unrenewed authority must
> lapse. **The membership set is the system's primary declarative desired state and was given no TTL, no
> default, and no silence semantics** — while `enhancements-required.md` C9 states the requirement
> verbatim: *"every declarative desired state names its converger, its clock, and what its silence looks
> like."*
>
> ### And the capacity-cap resolution is falsified by this document's own headline evidence
>
> Part 1 claimed *"the strategy already knows `max_positions`; a declared set that respects it can never be
> overruled."* The decision row quoted throughout as proof that the strategy speaks membership:
>
> ```
> 2026-08-14 13:35:01  decision  TRADING: hold 8 · enter 5 · exit 1
> 2026-08-14 13:35:02  risk      PSX   skipped PSX: position cap
> ```
>
> A conforming declared book of exactly 8 — and an entry refused one second later. Across all 7 decisions
> ever made: **11 cap refusals in 5 of 7 sessions**, and on 2026-08-06 *all six* entries were refused and
> the book fell from 8 names to 2. "Residual refusal" is the normal case, not the exception.
>
> Both repos recorded the session-dependent position cap as an open sub-gap *"orthogonal to membership"*.
> It is not orthogonal — it is the exact mechanism that overrules the declared set, and part 1 was
> accepted on the premise that it would not.
>
> **This is the correlated-error pattern in the artifacts rather than inferred:** the counterpart confirmed
> both the cap fix and the wind-down pacing within minutes, and in both cases the confirmation repeated the
> same unchecked premise.

| Finding | Source | State |
|---|---|---|
| `target_weight` contradicts entry-only sizing | both reviewers | **RESOLVED** — membership, not weight |
| A name the strategy has no opinion about | review 1 | **RESOLVED** — explicit `ABSTAIN` |
| Sidestep overloads weight 0 | review 1 | **RESOLVED** — `EXIT` ≠ `SIDESTEP` |
| No verification mechanism (SHADOW is exclusive) | review 1 | **RESOLVED** — in-strategy branch, not a second strategy |
| BCTROT-004 cannot be a proving ground | review 1 | **DISSOLVED** — MOMENTUM-002 is the proving ground |
| Durable intent store | review 1 | **RESOLVED** — the manager table, dispatched node-wide |
| Rollback path | review 1 | **RESOLVED** — the deployable unit is a `(cockpit, strategies)` PAIR |
| **The capacity cap silently overrules membership** | **review 2 — created by the fix** | **RESOLVED** — cap as input, REFUSED as an outcome, slots on terminal state |
| Shadow row collides with the idempotency index | review 2 | **RESOLVED** — the index is partial on `kind`; the shadow writes `kind='shadow'` |
| `ReadOnlyBroker` starves lifecycle SHADOW | review 2 | **CONFIRMS** the in-strategy design; retires lifecycle SHADOW |
| SIDESTEP maps to code nothing calls | review 2 | **STATED** — a specification, not production support |
| The wind-down has no home under binary membership | review 2 | **RESOLVED** — platform issues `EXIT` by the strategy's own published ranking |
| Entry sizing at less than a full book · session-dependent position cap · weights across `_resume` | kumo-strategies | **OPEN by mutual agreement** — orthogonal to membership, not closed by it |

**Agreed on both sides.** Every finding above was raised by one repo, verified independently against
source by the other, and resolved rather than escalated. Four positions reversed under evidence during
the process, three of them mine. `strategy_sleeve.target` is to be **dropped** — it produced a wrong
number three times tonight, including as the headline evidence of the issue meant to fix duplicated
facts.

- **Deciders:** the operator. Negotiated with kumo-strategies across a live peer session; every claim below traced
  to source in the running paper engine (Nautilus 1.229.0) or to this system's own logs and journal.
- **GitHub:** cockpit #346 #347 #348 #349 #350 · kumo-strategies #45–#52
- **Diagram:** https://claude.ai/code/artifact/a3a00687-5d0b-4c84-b60f-ab55b3ce27eb
- **Relates to:** ADR 0001 (execution ownership) — this narrows where the strategy boundary sits, and
  does not change the NETTING attribution model that ADR settled.

## Context

On 2026-08-18 the cockpit lost two trading sessions, refused an operator's emergency exit while having
already cancelled that position's protective stop, and was found holding a name it had correctly decided
to sell four sessions earlier. Reviewing every incident on record produced one observation:

> **Almost nothing here is a wrong algorithm. Nearly every incident is a failure of CORRESPONDENCE —
> between two records, between a component and its neighbour, between what a thing reported and what it
> did, between the artifact and its source.**

That framing is not mine. An independent cataloguer built its own list from 200 issues, 445 commits,
engine logs and eight Postgres tables **without reading this document**, derived its own taxonomy from
the evidence, and found **95 incidents across ten categories**. It is sharper than the "two shapes" claim
this ADR was originally built on, and it survives the counterexamples that one did not.

The original claim — *"every failure is one of two shapes … twelve incidents, both shapes, no residue"* —
is **withdrawn**. The twelve were never enumerated anywhere, an independent count found 95, and three
incidents fit neither shape, including the `slot` TypeError that started the entire investigation. A
claim of exhaustiveness that cannot be checked was doing the work of a proof.

What survives, and matters more: this is not a defect list. It is a missing architecture — and the
largest untouched category is one neither repo had named, **"built, configured, deployed, never
executed"** (14 incidents, never a fix campaign), which is invisible by construction because nothing
errors, nothing logs, and every test passes.

## Decision

### The five invariants

1. **One writer per fact. Everyone else derives.** No component keeps a second copy that can be written
   independently. Projections are rebuilt from the owner, never merged with it.
2. **Intent is declarative and durable, never a command.** Store what should be true, not the message
   meant to achieve it. A crash or a rejection loses nothing because nothing was encoded as one delivery.
3. **One controller per subject.** Whatever converges a position converges all of it — protection and
   exit together. Two loops with contradictory desired states oscillate; the gap between them is where
   the position is naked.
4. **Every action reaches an observed outcome, or it is still in flight.** In flight, terminal, or
   unknown — no fourth state. "Accepted" is not an outcome. Elapsed time is not an outcome. An unknown is
   resolved by asking the venue, never by assuming the optimistic case.
5. **Contracts are executable, and checked where failure is cheap.** An agreement between components is a
   test owned by the side defining the contract and run by the side satisfying it, at construction.

### The seam moves from execution to decision

Today the boundary sits **below** the decision engine: each strategy carries its own sizing, claims,
orphan sweep, reconcile and broker submit, so the seam carries orders and their plumbing. Every break at
it has been that plumbing — `slot` (scheduling), `jobs` (execution), `.journal` (recording), the budget
bus (sizing). None were decisions.

The seam moves to sit **directly below the decision engine**:

- **A strategy may rely on** venue truth (read-only), its own allocation, its lifecycle state, and market
  data. Nothing that lets it act.
- **A strategy may emit** a decision carrying `target_weight`, urgency and rationale, keyed
  `(strategy, session, slot)`. Never an executed order.

One platform-owned controller does sizing, actuation, observation, retry and terminal outcomes — once,
for every strategy.

### REVISION 2 — the strategy declares an ORDERED TARGET BOOK; the platform derives the rest

The third review disproved three of the five states and falsified the capacity fix against this
document's own headline row. Both collapse into one correction, and it makes the seam smaller.

**`HOLD`, `ENTER` and `EXIT` are not declarations. They are derivations.** The code already says so —
`engine.py:373` / `pgrunner.py:549`: `hold = (held - exits) | enters`. `hold` is the post-trade target
book *including every enter*, so the three are not disjoint and cannot be told apart without a second
fact: what is currently held. Under this ADR the platform owns positions, so the platform owns that fact.

So the seam carries **one ordered list** — the target book, in the strategy's own preference order — and
the platform diffs it against broker truth:

```
in target, not held      → ENTER      (derived)
in target, held          → HOLD       (derived)
held, not in target      → EXIT       (derived)
```

That removes the need for the claims table to distinguish them, which the review correctly noted the
deletion list was removing with nothing named to replace it.

Two states survive as **declarations**, because they carry information the platform cannot derive:

| Declared | Why it cannot be derived |
|---|---|
| `ABSTAIN(symbol)` | "No opinion today" is not visible in any diff. A name absent from the target book because the strategy has no bar for it is indistinguishable, from outside, from a name it decided to exit |
| `SIDESTEP(symbol)` | Sold *with the slot and cycle retained*. A diff sees only "not held" |

`SIDESTEP` stays in the vocabulary as a **specification** and does not ship in the first cut —
kumo-strategies recommended dropping it until wired, and that is adopted: **the seam ships with the
ordered book plus `ABSTAIN`.**

### REVISION 2 — capacity: the book is a PREFERENCE ORDER, so capacity never contradicts it

Part 1 of the previous resolution claimed a cap-conforming set could not be overruled. Falsified by the
row this document quotes as its own proof — `hold 8 · enter 5 · exit 1` at 13:35:01, `skipped PSX:
position cap` at 13:35:02 — and by 11 refusals across 5 of 7 sessions.

The premise was wrong in a way no amount of cap-passing fixes: **the binding constraint is discovered at
execution time.** Whether a slot is free depends on whether *this session's* sells cleared, which is not
knowable when the decision is made.

An ordered book dissolves the contradiction rather than arbitrating it. The strategy is no longer
asserting *"these eight names will be in the book"* — it asserts **"this is my order of preference;
take as many as you can."** The platform takes what capacity allows, in that order, and records
`REFUSED` with a reason for the remainder. Nobody is overruled, because nobody claimed the set was
achievable. Two facts, two owners, one deterministic intersection:

- the strategy owns **priority** — and already publishes it, as `ranking` in the decision detail;
- the platform owns **capacity** — count, capital, `budget_gate`, subscription, venue.

This also retires the `max_positions`/`n_hold` split the review found (two copies of one number in two
repos, agreeing by coincidence): under an ordered book **neither side needs the other's cap.** The
strategy submits its whole ordering; the platform stops when it runs out of room.

Slot reservation reverts to the safe direction the review identified — **reserve on submit, release on
terminal rejection** — which is what `pgrunner.py:735-745` already reasons out and test-pins for the sell
side. The earlier "consume on terminal fill" rule was wrong: a reservation and an outcome report have
opposite safe directions, and treating them alike would submit N buys against N free slots while the
first N were still in flight.

### REVISION 2 — silence, absence and staleness now have defined meanings

The review's lead finding: the seam's primary declarative state had no semantics for silence, and the
strategy is **deliberately silent on 4 of the last 11 sessions** (thin ranking, bar coverage, stale
sources, daily-loss halt) precisely because *"an EMPTY ranking is not a decision to sell the book."*
Under a converging platform, absent-means-exit liquidates the book and last-set-stands trades a five-day
-old opinion.

Three rules, and the asymmetry in the third is already this project's position rather than a new idea —
the operator's ruling that a decided **exit** retries until it fills, while an **entry** expires at its slot:

1. **A symbol absent from the target book is `ABSTAIN`, not `EXIT`.** Exits are declared by omission from
   a book the strategy actually published — never inferred from the absence of a publication.
2. **No publication for a slot ⇒ no membership action at all.** Not an empty book. The platform takes no
   entry and no exit; protection and in-flight intents are unaffected. And this must be a **positive
   published fact** — the strategy records "declined to decide, reason X" — never inferred from missing
   rows, which is the null-decision record this document already owed.
3. **A published book decays asymmetrically.** Fresh, it authorises everything. Older than N slots, it
   authorises **exits only** — exits do not rot, entries do. Past that it authorises nothing.

Rule 3 is the TTL this document derived three separate ways for protection suppression and then failed to
apply to its own primary state. `enhancements-required.md` C9 demanded it in writing: *"every declarative
desired state names its converger, its clock, and what its silence looks like."*

### ~~RESOLVED — the primitive is MEMBERSHIP, not weight~~ — see REVISION 2 above

The review's blocker: `target_weight` as a desired state **means convergence**, and this strategy's
measured edge assumes non-convergence — `pgrunner.py:812-815`, *"sizing is at ENTRY ONLY and never
rebalanced … measuring anything else would be measuring a strategy we cannot run."* Two independent
reviewers reached that separately. It kills `target_weight`.

**The resolution is in the data the strategy already emits.** A real decision row, verbatim:

```
slot open+5m │ "TRADING: hold 8 · enter 5 · exit 1"
```

The strategy already speaks **membership**. `dec.weights` is an entry-sizing detail applied to `enters`
only — never a portfolio target, never re-derived for held names. The seam should carry what the decision
already is, not a vector the engine has never produced.

**The platform converges on MEMBERSHIP, never on MAGNITUDE.** A position whose weight has drifted is not
an error to correct — the drift *is* the strategy, and correcting it would trade something that was never
backtested. What the platform converges on is binary and per-symbol: *is this name in the book or not*.

Five states, each with a distinct and non-substitutable platform behaviour:

| State | Platform behaviour |
|---|---|
| `HOLD` | In the book. **Do not resize.** Protection derived, nothing else acted on |
| `ENTER(size)` | Add, sized **once**, at this size. Never re-sized afterwards |
| `EXIT` | Leave the book: close the cycle, release the claim, free the sleeve capital |
| `SIDESTEP` | Leave the *position*, **keep** the slot, the cycle and the capital. `sidestep.py`: *"the name is sold, the slot stays reserved, and the position is restored on the reversal"* |
| `ABSTAIN` | **No opinion.** Do not touch. `pgrunner.py:533-546` holds a name with no bar rather than exiting it — *"selling because a feed hiccuped is not [valuable]"* |

This answers all three open blockers at once, and each state exists because production code already
distinguishes it:

- **B1 — a name the strategy has no opinion about** is `ABSTAIN`, an explicit state. Under a weight
  vector this was an absent key, and absent-means-zero liquidates the book on a feed hiccup.
- **B2 — convergence vs entry-only sizing** dissolves: membership converges, magnitude never does.
- **B3 — sidestep overloading zero** dissolves: `EXIT` and `SIDESTEP` are different states, not the same
  number, so one closes the cycle and frees capital and the other does neither.

Law 2 survives intact — these are declarative desired states, not commands. What is abandoned is the
claim that one scalar could carry all of them.

### ~~`target_weight` is the only primitive~~ — SUPERSEDED, see above

Enter, hold, exit and partial reduction are one primitive at different values; zero means flat. The
wind-down to a smaller sleeve stops being a separate mechanism and becomes the same decision with smaller
numbers.

### Protection is platform-derived, with no strategy input

`PROTECTED(w)` was in the first draft and is **cut**. No strategy has ever computed a protective width —
kumo-strategies `config.py:187`, *"the strategy has never had a stop (#30)"*; `stop_loss_atr` fires a full
exit, not a resting width. Cockpit's own audit is documented as deliberately strategy-blind: *"judged per
INSTRUMENT, not per strategy … this answers 'is the book covered', not 'is the attribution tidy'."* Stop
width is uniform risk policy, so asking a strategy for it invents a decision nobody makes.

### Transport: Nautilus orders carrying intent, not a per-session map

A map per session is a snapshot and cannot express a decision taken between sessions, an urgent exit, or
two slots in one day. The transport is `ExecAlgorithm`: **the PLATFORM submits the primary order** on the owning strategy's behalf, built through that
strategy's own `order_factory` so attribution is correct by construction; a node-registered algorithm owns
how and when it goes out. (Earlier text said the *strategy* submits it — that contradicted the seam and is
corrected here.)

The decision log is therefore **the Nautilus order cache** — already durable on Redis, already carrying
`INITIALIZED → SUBMITTED → ACCEPTED → FILLED/REJECTED/CANCELED`. No parallel ledger is built, which this
project already forbids.

## Evidence

| Claim | Source |
|---|---|
| The decision/actuation split already exists implicitly | kumo-strategies `backtesting/runner.py:73` constructs the adapter with **no** `session_runner`; `_session_coro` — the method that broke — is never exercised in backtest |
| Actuation is duplicated and diverging | `pgrunner.py` does `broker.equity()`, claims, orphan sweep, reconcile, submit; cockpit carries two gateways, `momentum.py` (410 lines) and `qc345.py` (674), the second documenting why it could not reuse the first |
| The seam's payload already exists | `dec.weights` — fractional conviction per name, summing to 1 — is produced by the decision engine (`pgrunner.py:803`) |
| Exits are already decision-shaped | `evaluate_exits → ExitPlan.exits: symbol → reason` |
| Trail state is already pure in/out | `exits.py`: *"STATE IS RETURNED, NOT MUTATED … the live path's durability requirement explicit instead of incidental"* |
| Law 4 is a read, not a build | `broker.py:100` returns `ok=true` at `INITIALIZED` — the first state of a five-state machine — and the journal records it as the last. `exec_action_log` 2026-08-14: `SELL 88 VCTR … {"ok": true}`, held four sessions |
| Law 3, measured | CGAU 2026-08-18 — exit cancels the stop 15:15:12Z, venue confirms 15:15:19Z, the protection reconciler re-places it 15:16:12Z |
| Law 1, measured | AEM held three quantities for one position: 18 in the Nautilus cache, 2 in `exec_position_state`, 0 at the venue |
| No ownership gate on execution algorithms | `algorithm.pyx` — `strategy_id` is attribution and event routing only, never gated. The MANUAL-001 dispatch scoping does not apply |

## Consequences

**Collapses rather than solves.** The operator-flatten ownership question (#348, ks#49) does not need an
answer: an ExecAlgorithm is node-registered, so the MANUAL-001 scoping that makes manager rows
undispatchable for MOMENTUM-002 positions simply does not apply.

**Deletion, cockpit:** two gateways → one thin adapter; `deferred_flatten` if the algorithm subsumes
delay and retry; the hand-rolled cancel-then-wait sequencing in `_handle_flatten_command`.
**Kept:** the manager framework for OPERATOR intents (PEAK, PYRAMID, stop-and-re-enter). The protection
reconciler is kept **unchanged during the migration and absorbed at the end of it** — see "the reconciler
is not exempt from Law 3" below.
**Added:** one ExecAlgorithm; `position_id` on submit; reserved-aware coverage.

**Deletion, kumo-strategies:** claims tracking, orphan sweep, reconcile-vs-broker (`pgrunner.py:325-345`);
`broker.py`'s `OrderResult`/`submit()` wrapper, and the `ok=true`-at-INITIALIZED anti-pattern with it;
the within-session `_MAX_SUBMIT_ATTEMPTS` retry hack; `_forced_exits`/LIQUIDATING's separate submit path.
**Kept untouched:** `evaluate_exits`, `decide()`, `score_panel`, `apply_gates`, TrailState, the configs.
**Backtest gets simpler**, not merely unaffected — it already skips the runner, so backtest and live
converge on one call once the seam sits below the decision engine.

**A cost that belongs beside the five laws:** one controller for every strategy is one blast radius for
every strategy. Today a bug in the strategy library's broker path breaks the strategies using it; under
this it breaks everything the platform runs, at once.

## Operator actions are first-class, not an afterthought

Operator, 2026-08-19: *"do not forget the manual peak, flatten, stop and rebuy (which I never saw working)
and peak."* An architecture that only serves systematic strategies would be the wrong one — MANUAL is a
strategy in this system and always live.

Under this ADR an operator action is a **decision like any other**: it carries a `target_weight` (flatten
is weight 0) plus urgency and rationale, and the same platform controller actuates it. That is what makes
the operator path stop being a second, weaker execution stack that has to re-solve cancel-then-exit by
hand — which is exactly how the 2026-08-18 flatten cancelled a stop and then refused the exit.

**What the manager rows say has actually happened** (production, all time):

| kind | APPLIED | CANCELLED | FAILED | reading |
|---|---|---|---|---|
| `peak_watch` | 40 | 3 | 1 | works, and is the source of #240/#245/#252/#254 |
| `stop_reenter_watch` | 5 | 13 | — | the WATCH half fires |
| `stop_reenter_rearm` | **0** | **0** | **0** | **no row of any state, ever — never instantiated** |
| `pyramid_watch` | **0** | **0** | **0** | **no row of any state, ever** — #257 |
| `deferred_flatten` | 2 | — | — | only ever for MANUAL-owned positions |

**Stop-and-re-enter has never worked, and now there is a reason rather than an impression.** The chain is
`stop_reenter_watch → stop_reenter_rearm`; the watch has applied five times and the rearm has **no row of
any state, ever**. It has never been instantiated — which is stronger than never completing, and rules out
the reading that it ran and failed. The handoff itself has never fired. This matches what the operator reports having
observed, and it is the same class as PYRAMID: a feature that reads as ARMED and healthy in the UI while
being structurally unable to complete.

Consequences for this ADR:

- The manager framework is **kept** for operator intents — it holds the durable row, atomic claim, event
  log, crash recovery and the human-in-the-loop leash, and none of that is replaced.
- Its **actuation** goes through the same platform controller as a strategy decision. A manager decides
  *when* and *what*; it never re-implements *how*.
- The chained kinds (`stop_reenter_*`) need their handoff verified end to end before the migration, not
  after — otherwise a rewrite inherits a chain that has never completed and nobody notices again.

## Migration constraint: the live book keeps trading

MOMENTUM-002 is live and holding, MANUAL is always live, BCTROT-004 and QC345-003 are registered. This
cannot be a big-bang cutover.

- **No step may leave a position unprotected**, including the intermediate states. The protection
  reconciler stays running and unchanged throughout — it is the safety net the migration is performed
  above, not a component being migrated.
- **Law 4 first (ks#51)**, because it is correct under both the old and the new boundary and makes every
  later step observable. Until it lands, a migration step that breaks execution looks identical to one
  that works.
- **One strategy at a time.** BCTROT-004 is registered, allocated, and has never produced a decision —
  it is the natural first mover, since nothing is at risk if its path is wrong. MOMENTUM-002 moves last.
- **The old path is DELETED, never left coexisting.** Operator, 2026-08-19: *"no. we need to clean up the old
  path. keeping it will lead to problems. We will build mistakenly on both."* This overrules an earlier
  coexistence clause that both repos had signed off. The objection is not live-trading risk during
  cutover — it is that a frozen-but-present old path is a standing trap. The freeze rule is a convention,
  and the next session to open `pgrunner.py` will not have this conversation in context.
- **SHADOW replaces coexistence for the decision half.** `lifecycle.State.SHADOW` already exists and is
  already shared: *"runs the full decision path and publishes what it WOULD do without submitting
  anything … how a strategy earns trust before it trades … validated against live data rather than a
  backtest."* Per strategy: run the new path in SHADOW against live data while the old path trades,
  compare, then cut over **and delete the old path in the same change**. No window in which both are
  live-capable and editable.
- **The comparison must be a recorded artifact, not a live eyeball.** SHADOW writes its would-be decisions
  into `exec_action_log`'s existing `(strategy, session, slot)` shape, so the cutover decision is a query
  and not someone reading logs on the day. This is the disagreement check the project already runs on.

### RESOLVED — the capacity cap must not silently overrule membership

A second review found that the membership fix **creates** a Law 1/3 violation rather than inheriting one,
and it is right. Verified, `pgrunner.py:823-827`:

```python
live = len(held_qty)
for sym in enters:
    if live >= self.limits.max_positions:
        await self.journal.write(RISK, f"skipped {sym}: position cap", session=session, symbol=sym)
        continue
```

Under `target_weight` this was arithmetic — scale the vector down, no fact contradicted. Under
**membership** the platform is unilaterally overruling part of a declared set, so two components now hold
different answers to *"is this name in the book"*, which is the seam's only fact. Nobody owns the final
set. That is worse than what it replaced.

Compounding it, the slot bookkeeping is gated on a non-terminal outcome — `live += 1` and `sent += r.ok`
sit on the submit path, and `r.ok` means *in flight*. A locally-accepted, venue-rejected buy has already
consumed a slot for the session.

**Resolution, three parts:**

1. **The cap becomes an INPUT to the decision, not a post-filter.** The strategy already knows
   `max_positions`; a declared membership set that respects it can never be overruled, which removes the
   contradiction in the common case rather than arbitrating it.
2. **Any residual platform refusal is a first-class REFUSED outcome** recorded against the intent row —
   never a `RISK` log line. If the platform must decline (capital, subscription, an unmappable symbol),
   the declared set and the actual set differ *on the record*, with an owner and a reason.
3. **A slot is consumed on a TERMINAL fill and released on a terminal rejection.** This is Law 4 applied
   to the cap, and it is the same defect as `ok=true`-at-`INITIALIZED` in a second place.

**The precise limit of part 1, stated so it is not read as doing more than it does.** Making the cap an
input removes the *arbitrary* overrule — the platform can no longer silently drop a name from a
conforming set. It does **not** remove the *timing* race, and cannot: the strategy declares against
`held` as it sees it at decision time, and a slot it counted on may still be occupied at execution time
because this session's exit has not filled. That is the session-dependent position cap kumo-strategies
flagged, and it stays open.

What closes the residue is part 2, not part 1. When the slot genuinely is not there, the platform records
a **`REFUSED`** outcome against the intent — declared and actual differ on the record, with an owner and a
reason — rather than dropping the name into a log line. So the guarantee is not "the platform never
overrules", it is **"the platform never overrules silently, and never on a stale count"**. Those are
different claims and only the second one is true.

### RESOLVED — the shadow row does not collide with the idempotency constraint

The review found that `uq_exec_one_decision_per_session` would reject a second decision row, and that
`decided_this_session` is path-blind — so a shadow writing first would make the live path skip itself.
Verified, and the constraint's own shape is the answer:

```sql
CREATE UNIQUE INDEX uq_exec_one_decision_per_session
  ON exec_action_log (strategy_id, session, slot) WHERE kind = 'decision'
```

It is **partial on `kind`**. The shadow writes `kind='shadow'`, which the index does not cover and
`decided_this_session` does not read. No migration, no path column, no weakening of the constraint that
makes retries safe — and the comparison stays a `SELECT` joining the two kinds on
`(strategy_id, session, slot)`.

### The lifecycle-SHADOW finding confirms the in-strategy design

The review notes `ReadOnlyBroker` does not forward `last_price`/`strategy_positions`/`position_entries`,
so every lifecycle-SHADOW entry degrades to *"no live price to size against"* — and the only two SHADOW
rows ever written read `hold 8 · enter 8 · exit 0`: eight entries decided, none sizeable.

That is not a problem for this ADR; it is confirmation of it. The in-strategy shadow is a branch inside
the **live** strategy, using its real reads. The finding retires lifecycle SHADOW as a verification tool
entirely rather than damaging the replacement.

### SIDESTEP specifies behaviour that does not yet exist — stated, not hidden

`evaluate_sidestep` exists and does what the ADR describes. **Nothing calls it.** It is imported by
exactly its own test file, `SidestepConfig` is absent from `config.py`, and it is wired into neither
`pgrunner`, the engine, the backtest, nor either gateway.

So `SIDESTEP` is a *specification*, not a mapping onto working code, and this ADR says so rather than
implying production support. It is also — pointedly — the **built-never-executed** category recurring
inside the fix meant to address that category. Recorded as such.

### RESOLVED — the wind-down has a home without breaking binary membership

Binary membership cannot express *"reduce toward target"*, and `strategy_sleeve` shows
`MOMENTUM-002 | target 0.00 | actual 67,111.69` live right now.

It does not need to express it. **Reducing to a capital target is a platform concern, not a trading
decision** — the platform knows the sleeve, and the strategy has already published the only input the
choice needs: the decision row carries `ranking`. So the platform issues `EXIT` against the
lowest-ranked HELD names, using the strategy's own ordering rather than inventing one.

**This is not a new authority.** It is the channel `_forced_exits`/LIQUIDATING already use and the
strategy already expects — the platform knows something the ranking does not. Two conditions make it
legitimate rather than the silent-contradiction shape the cap finding exposed:

1. **It is recorded with a distinguishable reason**, exactly like the `REFUSED` outcome — never a log
   line. An operator must be able to tell a platform-forced exit from a strategy exit at a glance.
2. **It is PACED, and the pacing is a hard bound rather than an implication.** kumo-strategies caught
   that the first draft said "until the sleeve is inside target", which would liquidate the entire gap in
   one session. #350/ks#46 asked for *"over N sessions, capped per session, worst-ranked first"* —
   deliberately gradual — and that pacing is the requirement, not a detail.

**The bound is in NAMES, not dollars**, and that follows from the seam rather than from taste: a dollar
cap would force a partial exit, a partial exit is magnitude, and magnitude is precisely what a membership
seam does not carry. One name is the smallest unit the vocabulary has.

**Default: one name per session**, raisable by setting, per the standing rule that new gates open slowly.
Its ceiling has a principled form — **the platform's forced reduction must not exceed the strategy's own
natural turnover**, or the platform becomes the dominant actor in a book the strategy is supposed to be
running. Observed turnover from the decision rows is 1–5 names per session (`hold 8 · enter 5 · exit 1`,
`hold 4 · enter 1 · exit 5`), so one is comfortably inside it.

**Correcting the size of the problem, because the wrong number has now propagated three times.** The gap
is **$47,111.69**, not $67k: `target 20,000 · actual 67,111.69 · must_reduce 47,111.69`, read live from
`GET /strategies`. The `$0 target` figure comes from `strategy_sleeve.target`, which
`budget_store.py:47-48` documents as **vestigial and NOT read** — settings owns the target, the table owns
`actual`, one writer each. That column has now been cited as evidence in ks#46's headline, in a reviewer's
report, and in a peer message. It is a Law 1 violation *about* a Law 1 violation, and the column should be
dropped rather than left to keep misleading readers.

Membership stays binary, the strategy is not asked a question it has no opinion on, and #350/ks#46 get
the actor they have never had.

### REVISION 2 — the shadow is on the ORDER SET, not the decision

The third review found the verification mechanism structurally unable to detect anything: both deletion
lists keep the decision functions **untouched**, so shadow and live call the same code on the same panel.
No diff is the *designed* outcome and is indistinguishable from the shadow never having executed. That is
this project's own rule — a test that cannot fail carries no information — aimed at its own migration.

The diagnosis is right and it locates the error precisely: **the shadow was on the wrong artifact.**
Shadowing the *decision* verifies code nobody is changing. What is changing is the **translation from a
decision into orders** — sizing, cap filtering, claims, submission — all of which move from the runner to
the platform.

So the comparison is the **order set**:

```
same decision row D
  ├─ OLD path:  D → sizing + cap + claims → orders actually submitted   (already recorded)
  └─ NEW path:  D → ordered book → platform diff vs broker → orders it WOULD submit
                                                              (computed, recorded, never sent)
diff the two order sets
```

This is falsifiable in a way the decision comparison was not, because the two paths run **different
code** to get there. A no-diff result is now evidence rather than a tautology.

Three properties it must have, and the third is the one the review's rule demands:

1. **Side-effect free.** The shadow runs the translation only — no `_save_state`, no `_drop_state`, no
   lifecycle write, no per-symbol journal rows. One comparison row per `(strategy, session, slot)`.
2. **Comparable by query**, one row against the recorded orders for the same key.
3. **It must be able to fail loudly for the right reason.** Assert the shadow produced a **non-empty**
   order set on every session where the live path produced one. A shadow that silently computes nothing
   is the failure mode the review named, and it is caught by a positive assertion rather than by the
   absence of a diff.

That also removes the `_resume` collision: the shadow writes no per-symbol `phase: result` rows, so
`_resume`'s unfiltered `tail(400)` scan has nothing of the shadow's to misread.

### ~~RESOLVED — verification is an in-strategy shadow branch, on MOMENTUM-002 itself~~ — superseded above; the branch is still in-strategy, but it compares ORDERS

The review's blocker: SHADOW is `TRADING`'s pause state, one lifecycle row per strategy, and a twin
cannot be registered (`order_id_tag` unique node-wide; `external_order_claims` exclusive). So
shadow-alongside-live is not implementable, and BCTROT-first proves nothing because BCTROT has no
capital, no lifecycle row and zero rows of any kind.

**The resolution is that the shadow is not a strategy at all.** It is a second code path *inside the one
running strategy*: both decisions computed, both logged, one submitted. No second Nautilus registration,
so none of the three exclusivity walls apply — no lifecycle row, no `order_id_tag`, no claims.

Three consequences, and the third is the one that matters:

1. **The comparison is a query, not a log read.** Both decisions write to `exec_action_log` — which
   already carries `(strategy_id, session, slot)` and a `jsonb` detail — with `kind='shadow'` (one discriminator only — see the order-set shadow above).
   Agreement or divergence is then `SELECT`-able per session, and a cutover decision rests on a diff
   rather than on someone reading logs on the day.
2. **The safety property is directly testable.** Assert the shadow branch never reaches `submit_order`,
   via a broker stub that **raises** rather than records. That is a double which rejects what production
   rejects — the discipline this repo already writes down.
3. **BCTROT-first is no longer needed, and the sequencing problem dissolves.** The proving ground becomes
   MOMENTUM-002 itself — the strategy that actually matters — running both paths for N sessions with only
   the old one able to submit. That is strictly better than proving a controller on a strategy that holds
   nothing and then using it, unproven in anger, on the one that holds $67k.

What remains unshadowable is the **actuation** half, and that is unchanged: no venue interaction can be
simulated, so it is measured with probe scripts against the paper account, and the protection reconciler
holds a stop on every position throughout.

### RESOLVED — the durable intent store is the manager table, and it fixes the scoping too

`ExecAlgorithm` cannot be the store: routing happens once at `submit_order`, `_reset` clears spawn state,
and nothing re-delivers a primary after a restart. But building a new store would repeat the
parallel-ledger mistake this project already forbids.

**It does not need building. `api/managers.py` already is one** — durable row, atomic `ARMED → APPLYING`
claim, event log, crash recovery of orphaned `APPLYING`, and the human-in-the-loop leash. 64 rows in
production. #178 proposed exactly this generalisation in July 2026 and it was parked.

**And it is not a parallel ledger, by Law 1.** Nautilus owns *what orders and positions exist*; the
manager table owns *what we intend*. One writer per fact, no second copy of anything Nautilus holds. That
distinction is the whole reason it is legitimate and a second order-state store would not be.

Two changes make it work, and both are verified reachable:

1. **Dispatch acts on any row**, not `str(self.id)`-scoped.
2. **Actuation builds the primary through the OWNING strategy's `order_factory`**, reached via
   `Trader.strategies()` — both confirmed present in 1.229.0. Attribution is then correct *by
   construction*: `strategy_id` on the primary and on every spawned child is the owner's, which is
   exactly what the orphan-adoption section requires.

**This retires the reason #348's guard existed.** The guard blocked MANUAL-001 from attaching for another
strategy because MANUAL would have built the order and corrupted attribution. Build it through the owner's
factory and that corruption cannot occur, so the predicate becomes *"does this node host that strategy"*
rather than *"am I that strategy"* — a check that is true for every position in the book.

### RESOLVED — the rollback is a PAIR, and today the image cannot name half of it

"No fallback" is a design rule about runtime mechanisms. It is not "no rollback", and conflating the two
is how a 09:35 failure becomes a day of manual trading.

The obstacle is concrete. `deploy/Dockerfile.backend:35-39` installs kumo-strategies from a local build
context, so a cockpit-only revert pairs new cockpit code with whatever strategies revision happens to be
on disk — recreating exactly the version-skew class that produced #346. And the running engine cannot
tell you which revision that is. Its own boot line, verbatim:

```
build: cockpit=c327707 strategies=unknown
```

So: **the deployable unit is the PAIR `(cockpit_sha, strategies_sha)`**, stamped together, and rollback is
redeploying the previous pair — never one repo. #329 added `KUMO_GIT_SHA` for cockpit; the missing half is
stamping the strategies revision, which `uv.lock` cannot supply because the installed wheel reports
`file:///tmp/kumo-strategies` (#329, #J1).

Until both halves are stamped, there is no rollback — only a redeploy that hopes.

### The actuation half has no SHADOW, and cannot fully have one

Stated plainly because implying otherwise would be the most dangerous sentence in this ADR.

A decision can be computed and withheld — the computation *is* the thing, so SHADOW loses nothing. An
actuation's substance is the **venue interaction**: when the reservation releases, how long a cancel takes
to confirm, what the rejection reason actually says, whether the fill is partial. Shadowing that exercises
the code and skips the part that breaks. Every failure behind this ADR lives in the skipped part —
VCTR's rejection, AEM's 52-of-54 partial, CGAU's 7-second cancel-confirm, #245's `available: 0`. A shadow
ExecAlgorithm would have produced a clean dry run for all four.

`DryRunBroker` is not the answer either: it reports an empty account, which is why `SessionGateway`
deliberately uses `ReadOnlyBroker` instead. Fake reads make fake decisions; fake venue responses would
make a fake actuation.

The proving ground is therefore three things, none of them a shadow:

1. **Measure the venue, do not simulate it.** Probe scripts against the paper account for cancel-confirm
   latency, reservation-release timing and the rejection-reason vocabulary to classify on — precedent
   `scripts/probe_trailing_replace.py`, and the standing rule *"unverifiable by inspection → measure it."*
   Those measurements are the constants the algorithm is built from, before it touches a position.
2. ~~**A scoped first cutover — one symbol.** The algorithm owns actuation for a single position while
   the old path owns the rest, deleted in the same change once it holds. This IS a two-path window and is
   named as one; it is scoped and time-boxed rather than a standing architecture.~~
   **STRUCK — see "No fallbacks — including the ones we like" below.** Two live-capable paths over one
   resource is the pattern being removed, scoped or not. Retained only so the reasoning is not re-derived.
### No fallbacks — including the ones we like

Operator, 2026-08-19: *"i said no fallback. it needs to work principled. no fallbacks, proper clean up"* and
*"fallbacks fire back."*

That is not caution about the migration; it is Law 3 applied to the migration **process**. And it is the
pattern behind every incident in the evidence table: the reconciler re-placing a stop the exit cancelled;
the budget bus as a second transport for a number that already had one; two gateways diverging; VCTR
unretried because `ok=true` was terminal in one path while the truth lived in another. Each is a second
mechanism standing beside the first "just in case", and in each the second mechanism **is** the defect,
not the safety margin.

This kills the scoped one-symbol cutover proposed above. Old and new both live-capable over one position
— even time-boxed, even with an exclusion marker making it "safe" — is still two controllers on one
resource. The exclusion-marker work item goes with it: it was bridging machinery in service of a pattern
that should not exist. Both are struck rather than deleted from this document, so the reasoning is not
re-derived later.

Replaced by:

- Build the new path **complete per strategy**, never symbol by symbol.
- Verify with things that require no dual ownership: SHADOW for decisions; probe scripts for actuation,
  measured against the **paper** API with orders that touch no live strategy's position. This system runs
  on Alpaca paper, not live capital — which is exactly the environment "measure it, don't simulate it"
  wants, and is what makes an atomic cutover affordable here.
- Cut over **atomically per strategy** — old path stops, new path starts, one watched deploy, outside
  market hours.
- **Delete that strategy's old code in the same change.** Never deferred.
- **The sequencing IS the verification.** BCTROT-004 first proves the controller in real production use
  with zero position at risk; MOMENTUM-002 last means the controller has already been proven on another
  strategy under real conditions before it touches the one that matters. The proof comes from ORDER, not
  from two paths running side by side.

### The reconciler is not exempt from Law 3

kumo-strategies argued the protection reconciler is not a fallback in the rejected sense — an independent,
permanent risk layer rather than a second copy of the execution path. Half right, and the half that is
wrong matters.

It is independent in *intent*. It is not independent in *mechanism*: it and the exit path contend for the
same shares, and **that contention is CGAU** — the canonical entry in this ADR's own evidence table. My
earlier phrasing, "the reconciler is the net", is fallback language and should not have survived a
document that rejects fallbacks.

So the end state is not "protection reconciler kept forever alongside the controller". Under Law 3,
protection is a **desired state derived and actuated by the single controller that owns the position** —
still platform-computed with no strategy input, still strategy-blind risk policy, but no longer a second
loop racing the first.

**Resolved after pushback, and the resolution is better than either starting position.** kumo-strategies
argued that CGAU proves protection and the exit path need a **shared source of truth**, not the same
process — and that is correct. Law 3 forbids two *contradictory desired states*; it does not require one
scheduler.

The failure mode they named is real and empirical rather than hypothetical. The new controller will be
the most complex code in this system — sizing, retry, urgency, spawning, cancel-then-confirm against a
live venue — and this ADR's own history is of being wrong about exactly that layer: `market_exit`
atomicity, `manage_contingent_orders`, and an orphan-adoption collision neither document anticipated.
Meanwhile the reconciler's record is that in **every** incident so far it recovered from something else
failing, precisely because it did not share that thing's fate: CGAU, and both flatten incidents.
Absorbing it into one process trades a coordination bug in normal operation — real, and fixed by the
shared signal — for shared failure in abnormal operation, which is unmeasured.

So the answer to "what does absorbed mean" is **one authority, not one process**:

- **The controller owns the desired protection state.** It is the single authoritative answer to "should
  this position be protected right now, and how wide".
- **The reconciler becomes an independently-scheduled ENFORCER of that state.** It reads the authority; it
  never computes a competing one. It keeps its own clock, its own process lifetime, and its own broker
  reads, so it survives the controller's failure.
- **CGAU is fixed by the shared signal, not by the merger.** Protection stops re-placing a stop the exit
  just cancelled the moment it reads one truth instead of deriving a second.

### The discriminating rule — what makes something a fallback

the operator's "fallbacks fire back" needs a test, or it becomes a mood. This is the test:

> **A second mechanism is a fallback if and only if it can compute a desired state that contradicts the
> first. Independent scheduling against a shared authority is not a fallback.**

It explains every case tonight without special pleading. The budget bus was a second transport for a
number that already had one — a second potential truth. Two gateways were two implementations of one
behaviour, free to diverge, and did. `ok=true` alongside the venue's own state machine was a second
answer to "did this fill". The protection reconciler as it exists **today** is a fallback by this test,
because it derives its own answer to "should this be protected" with no knowledge of an exit in flight.
The same reconciler reading the controller's desired state is not, because it has no independent opinion
left to contradict with.

Independence of *fate* is a safety property. Independence of *opinion* is the bug.

#### The rule's seam: stale authority, not unreachable authority

**Unreachable** is the easy case and the test covers it. A contradiction needs two answers to the same
question at the same time; an unreachable authority asserts nothing, so a pre-agreed degraded default is
orthogonal to it rather than competing with it. The enforcer fails **closed** — unreadable means protect,
because a spurious stop costs a replaced order and a missing one costs a position.

**Stale is the dangerous case**, and it is worse precisely because nothing is broken. Authority reachable,
answering, and wrong: a suppression flag left "on" because the controller crashed mid-exit and never
cleared it. The enforcer defers exactly as designed, no rule is violated, no contradiction exists — and
the position sits unprotected because nothing ever said the exit was abandoned.

**The fix is a TTL, not a boolean.** Suppression that is not actively renewed within a bound lapses back
to "protect" on its own. That converts stale-authority into unreachable-authority in the only dimension
that matters: the enforcer's rule stays "defer to the authority, or to nothing", and "nothing" now
includes "an answer too old to trust".

This requirement was reached three independent ways tonight, which is the strongest evidence available
that it is right:

1. **The codebase already solved it once.** `engine_node.py:192-201`, on the existing pending marker: *"a
   PERMANENT marker then suppresses every future attempt while broker REST shows no open stop — the
   position naked forever and silent about it. So the marker EXPIRES."*
2. **By inspection**, in the review that first examined the suppression design: an exit-intent flag that
   never clears silently disables protection, and no expiry had been designed for it.
3. **By the formal test above finding its own blind spot** — a case where the rule is satisfied and the
   outcome is still wrong.

- **During the migration** the reconciler stays exactly as it is — for a strategy not yet cut over there
  is no authority to read. A sequencing fact, not an exemption.
- **At the end** it reads the controller's desired state rather than deriving its own, and otherwise keeps
  running as it does now: separate schedule, separate failure domain, same broker-truth inputs.

### Blocking dependency for a live-book cutover: the orphan sweep would adopt the new path's positions

Found by kumo-strategies by tracing `pgrunner`'s reconcile, and verified here in the Nautilus source
rather than taken on either side's word. `spawn_market` constructs its child as:

```
return MarketOrder(
    trader_id=primary.trader_id,
    strategy_id=primary.strategy_id,      # <-- the OWNING strategy, verbatim
    ...
```

So a position actuated by the platform ExecAlgorithm on MOMENTUM-002's behalf is attributed by Nautilus
to MOMENTUM-002 — correctly. But the old path's reconcile computes:

```
orphans = {s2: q for s2, q in (mine or {}).items() if s2 not in claims and q > 0}
```

where `mine` is what **Nautilus** attributes to the strategy, not what `pgrunner` submitted. The new
algorithm writes no row to the claims table, so the old path sees a position it believes it opened and
has no record of, and **adopts it** — a mechanism that exists specifically to bring an unclaimed position
under active management.

**That is Law 3 recreated inside the migration meant to eliminate it**: two controllers on one position,
each acting independently. `EXCLUDE`/`PIN` do not cover it — they govern pool candidacy, not claims or
reconcile.

- **Does not affect BCTROT-004**, which holds nothing; there is no position to adopt. BCTROT-004 first
  therefore remains correct and unblocked.
- Blocked the scoped cutover on a strategy with a live book (that plan is now STRUCK; this finding is retained because the same collision applies to any cutover on a strategy holding positions).
- kumo-strategies owns the fix: `pgrunner`'s reconcile needs "claimed, externally managed" as a state
  distinct from both "mine, unclaimed — adopt it" and "not mine, foreign — ignore it".

3. ~~**The protection reconciler is the net, untouched throughout.**~~ **SUPERSEDED — see "the reconciler is not exempt from Law 3" below.** "The net" is fallback language and "migrated last, or never" is wrong: the reconciler becomes an independently-scheduled ENFORCER reading the controller's desired state. It stays unchanged only *during* the migration. What follows is retained for its measured detail. Every position keeps a stop re-derived
   from broker truth every tick regardless of which actuation path is exercised. That property is what
   makes (2) survivable and is the reason the reconciler is migrated last, or never.
- **Deploys are watched.** Every step ships outside market hours, with `assert_conforms` raising at
  construction so a broken wiring refuses to boot rather than dying at the next open.

## Freeze list — code slated for deletion does NOT get fixed

Operator, 2026-08-19: *"clean up plan included — so we don't change old code later."*

The deletion lists above are not only an end state; they are a **do-not-touch list from today**. Effort
spent repairing a component this ADR removes is effort spent twice, and worse, it makes the removal
harder by adding behaviour someone then has to port. This section exists so that open issues are not
worked in the wrong order by whoever picks them up next.

| Open work | Verdict | Why |
|---|---|---|
| `_submit` passes `position_id` (#347) | **DO NOW** | Any submit path needs it, including the ExecAlgorithm one. Survives the migration unchanged. |
| Reserved-aware coverage in `plan_protection` (#347) | **DO NOW** | The protection reconciler is explicitly kept and unmigrated. |
| Outcome feedback (ks#51) | **DO NOW** | Correct under both boundaries. Makes every later step observable. |
| Protocol + `assert_conforms` (ks#45) | **DO NOW** | Hardens whichever seam exists. |
| `_await_shares_available` timeout tuning (#347) | **DEFER — port, do not tune** | It lives in the flatten sequencing that is slated for deletion. The *logic* moves into the ExecAlgorithm; measure the venue latency once and apply it there. |
| `_reserve_attach_command` scoping guard (#348) | **REOPENED — verdict withdrawn** | The DO-NOT-FIX rested on "an ExecAlgorithm is node-registered, so the scoping dissolves". This document disproves that in its own orphan-adoption section: `ExecAlgorithm` has **no `order_factory`**, so a `Strategy` must build every primary, and `spawn_market` propagates `strategy_id=primary.strategy_id` verbatim. Whoever builds the primary for an operator flatten of a MOMENTUM-002 position IS the MANUAL-001 scoping question, unchanged. It also never addressed the second rejection path — foreign venue state, resting exit orders this system did not place. |
| `deferred_flatten` repairs | **DO NOT FIX** | Subsumed if the algorithm owns delay and retry. |
| Hand-rolled cancel-then-wait in `_handle_flatten_command` | **DO NOT FIX** | Deleted. Its behaviour is what the ExecAlgorithm must reproduce, so it is a specification, not a maintenance target. |
| PEAK defects #240 #245 #252 #254 | **PARTIAL — fix only what survives** | PEAK is KEPT as operator intent, but its *actuation* moves. Trigger logic, chain state and the leash survive and may be fixed. Anything that cancels or submits orders directly is being replaced — do not repair it. |
| `stop_reenter_rearm` never instantiated | **DO NOW — diagnose, do not rebuild** | Understanding why the handoff never fires is required before migration (see above), but any fix belongs on the new path. |
| Second gateway (`qc345.py`) divergence | **DO NOT FIX** | Both gateways collapse into one adapter. |

**Rule of thumb:** if a component appears in a deletion list, the only legitimate work on it is
*understanding* it well enough to reproduce its behaviour on the new path. Bugs in it are specifications
for the replacement, not tickets.

### The freeze stops at the observation, not before it

kumo-strategies' refinement, and it is the right cut: `broker.py`'s `OrderResult`/`submit()` wrapper is
**NOT frozen**, because repairing it *is* ks#51. The freeze applies to **retry and actuation downstream of
an observation**, never to making the observation correct. An unobserved outcome is wrong under any
boundary, so fixing it is never wasted work.

### Ownership questions closed by tracing, not by assumption

| Question | Answer | How |
|---|---|---|
| Is urgency joint? | **Cockpit alone.** | `_forced_exits` reads `pool.must_liquidate` (operator override) and LIQUIDATING is an operator-only lifecycle transition. `evaluate_exits` has no concept of urgency at all, so nothing in a decision engine computes it. The vocabulary reserves an optional urgency field for a future strategy-native case; none exists today, so it is a reservation, not a design. |
| ExecAlgorithm before the adapter? | **Yes.** | An adapter emitting decisions nothing consumes is a producer with no consumer — the shape that produced the budget bus. |
| Is `stop_reenter_rearm` cockpit's alone? | **Yes.** | No re-entry concept exists in `evaluate_exits`/`engine.py`; a stopped-out name returns only via ordinary re-ranking. `stop_reenter` lives entirely under MANUAL-001, which runs no ranking engine. |
| Is `pyramid_watch` cockpit's alone? | **Yes** — checked here rather than assumed, because kumo-strategies explicitly declined to answer it on either side's word. | Its trigger reads a `driver_instrument_id` (an operator-typed ticker), ADX rollover, own fade and breakout confirmation, with R derived once from the position's existing bracket stop. No ranking-engine input anywhere. |

## Open exposure during the migration — needs the operator's decision

ks#51 delivers **observability** of a failed exit. It does not deliver **recovery** — the retry that would
act on that observation is item 5, and everything that would have hand-rolled it is frozen.

So between ks#51 landing and the ExecAlgorithm existing, **a rejected exit is visible but not
self-healing.** That window is strictly better than today, where the same exit is neither visible nor
healing — VCTR sat four sessions unnoticed. But it is not zero exposure, and it is a deliberate choice
rather than an oversight.

Two options, and this is not ours to pick:

- **Accepted exposure** — the window is short, the protection reconciler still holds a stop on every
  position throughout, and a visible stuck exit is a large improvement on an invisible one.
- **Manual fallback for the migration's duration** — a stuck exit raises to the operator (the existing
  Telegram transport, #199, is built and wired to nothing), and a human closes it by hand until the
  algorithm can.



## DEFECT IN THIS DOCUMENT — the retry design would be denied before it reached the venue

Found 2026-08-19 by kumo-strategies' code review of their own `#51` fix, and verified here in the
Nautilus source. **Marked rather than silently edited**, because it is the sixth thing tonight that looked
done and was inert — and the first where the inert thing was a *design* rather than code.

`trading/strategy.pyx:865-868`:

```python
# Check for duplicate client order ID
if self.cache.order_exists(order.client_order_id):
    self._log.error(f"Cannot submit order: duplicate {repr(order.client_order_id)}")
    self._deny_order(order, f"duplicate {repr(order.client_order_id)}")
```

A resubmission carrying the same client order id is denied **locally, before the venue**. Every version of
this ADR's retry-until-filled design — durable intent, bounded attempts, classified rejection reasons,
`market_exit` as the inner loop — **never says the id must vary per attempt.** So attempt 2 would be
denied by our own risk engine, and the denial journals identically to a genuine venue rejection: the loop
would record itself as *"tried, refused"* while never reaching the venue at all.

**Four independent reviews read this document and none caught it.** A code review of one repo's actual
diff did.

### It is not hypothetical — cockpit has already been bricked by it

Issue **#295**, before this work. `_protection_coid` is deterministic for idempotency. A retry after a
venue rejection resubmitted the same coid, the RiskEngine denied it, and the order then carried **two
terminal events**. On the next boot `load_orders` replayed them, `InvalidStateTrigger: REJECTED -> DENIED`
killed `TradingNode` construction, and every subsequent start died the same way — made permanent by the
durable cache, cleared by hand with `redis-cli`.

### What any retry design has to do instead

Vary the id per attempt **while keeping the idempotency property that made it deterministic**. The pattern
kumo-strategies adopted is the one to copy rather than invent a second: an attempt component folded into
the id only when non-zero, so `attempt=0` stays byte-identical to every existing order and nothing else
changes.

Cockpit's own deterministic retry surfaces — `_protection_coid` and `FL-{cid[:20]}` — carry the same
latent defect and are recorded in `enhancements-required.md` rather than changed here: protection is the
mechanism that has never failed, and it is not being altered at the end of a long session on the strength
of a design note.

## What this does NOT solve

**Nautilus has no primitive for "resolve a competing reservation before acting."** Traced to the adapter:
`spawn_market` / `reduce_primary` reduces the primary's own `leaves_qty` as *its own* children fill —
TWAP-style slicing of one order. It never touches an unrelated resting order, so it does nothing about a
protective GTC stop reserving the shares an exit needs. `ExecAlgorithm` is a **home** for that logic, with
durable spawn bookkeeping and restore-on-reject for free. It is not the logic.

Three Nautilus natives were claimed and withdrawn during this design — `market_exit` atomicity,
`manage_contingent_orders`/OUO, and this. Each time from reading a primitive's shape and inferring it
solved the problem, without tracing to the specific failure. This is the rule CLAUDE.md already states.

## Open, and required before this is Accepted

1. **Urgency cannot be a metadata field.** `_forced_exits` and LIQUIDATING deliberately bypass the slot
   idempotency gate and fire mid-session. That is control flow, not a tag; make it a field and the bypass
   is lost silently — the exact failure shape this ADR exists to prevent. Needs explicit design.
2. **A null decision has no order to ride on.** An order-shaped log cannot record "considered, declined".
   The session outcome journal stays as a separate record, and the two must not be collapsed.
3. **Order construction defaults** (`TimeInForce.DAY`, `reduce_only` on exits) are uniform across the two
   strategies running today. Promoting them to platform policy is an assumption about those two, not a law.
4. **Law 4 ships first regardless.** No controller can converge on an outcome that was never recorded, so
   nothing else here is testable until it exists (ks#51). It is correct under any boundary.
