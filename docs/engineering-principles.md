# Engineering principles

The rules this codebase is built and reviewed by. They were written one defect at a time; each section
names the defects that produced it, because a rule without its cost reads as a preference. The defects
are described by shape rather than by ticket number — the tracker they were filed in is private, and the
shape is what transfers.

This is the engineering content of the project's operating manual. The manual itself also carries
day-to-day operating instructions for a specific deployment and stays private.

## 1. What this is

An operator platform for automated equity strategy lanes on [NautilusTrader](https://github.com/nautechsystems/nautilus_trader),
running against Interactive Brokers and Alpaca **paper** accounts. Five layers:

| concern | what it holds |
|---|---|
| runtime | a Nautilus `TradingNode` hosted in-process by FastAPI; lane registry, slots and cadence, one-shot history, the market-view contract, end-of-day capture |
| execution | exec clients per venue; ownership guard, budget gate, protection stance, venue reconciliation, surplus eviction |
| operations | the settings plane, the health plane, the alert record, deploy/verify/precheck, the instance model |
| strategies | the lane *wrappers* live here (`backend/strategies/` — one module per lane: registration, decision slots, lifecycle state, session budget); the strategy *logic* and the runtime contract it runs on are published separately as `kumo-trading-strategies` |
| cockpit | the Next.js UI — render-only, over REST and WebSocket |

These are concerns, not directories: `backend/api/` holds the first three. The UI is the face; the
platform is the thing. **It is not authenticated, not multi-tenant, and not validated for real
capital.**

## 2. Architecture invariants

- **NautilusTrader is a pinned library, never forked.** Extend through out-of-tree `Strategy` and adapter
  subclasses. LGPL-3.0 lets that code carry its own licence as long as the core is linked, not modified.
- **Market data comes from the broker. No third-party feed.** An instance's data provider equals its
  execution provider. Venue market-data limits are worked around with **bar-type choices** — subscribe only
  the granularities the venue serves directly — never by adding a vendor. Much of the live plane is
  display and order-ticket support rather than lane decisions, so its gaps are tolerated rather than
  fixed with a second source. (This superseded an earlier decision to use a separate data vendor; the
  dead code and config for that vendor were left in place and are read by nothing — a reminder that a
  decision reversed in prose must be reversed in the tree.)
- **The UI is render-only.** Data reaches it over WebSocket, no polling. The vendor dies at the adapter
  seam; the message bus carries pure Nautilus domain objects, and the bus serializer *is* the DTO layer.
  A display `Actor` subscribes in-process and forwards to the socket; when the UI needs its own process,
  the bus flips to external Redis streaming and a ~50-line bridge replaces the actor — config, not a
  redesign. Data and execution must share one cache and are never split across nodes; a data-only display
  node shares nothing and may be separate.
- **Per-strategy ownership.** Each strategy is a Nautilus strategy with its own `StrategyId`; NETTING
  position ids `{instrument}-{strategy_id}` give each its own net position and per-leg P&L. A
  cross-strategy coordinator, not a master strategy, arbitrates. **Broker net is the only hard
  reconciliation anchor**; the per-strategy split is unverified by the broker; external or manual activity
  is claimed or quarantined. A **trade cycle** (open → closed, spanning flats) is an engine projection
  above native positions — P&L is derived from native, never kept in a parallel ledger.
- **Cross-strategy risk is portfolio-wide.** Heat caps and correlation/sector caps span every strategy,
  and ETF holdings decompose into sector exposure.
- **No autonomous BUY without an explicit human unlock. Every new automation gate defaults to `False`.**
  Opt-in only.
- **Secrets live in the environment**, sourced at deploy time from one managed place. Never committed,
  never in a per-worktree file.
- **The instances repository defines the instance — the platform does not.** Identity, providers, ports,
  gates, pinned versions and the settings values that decide what trades live in `instances/<name>/`.
  Never infer an instance's configuration from code, from a default, or from a sibling instance.
  - An env file is authoritative for a knob only because **three things line up**: declared there,
    forwarded by compose into *every* process that reads it, and honoured by the loader. Break one and
    the file still looks authoritative. Two knobs were declared, documented and exported for weeks and
    read by nothing; the fix forwarded them and created the sibling defect — compose interpolates an
    unset variable to the **empty string**, so `environ.get(k, default)` returns `""` and the default
    never fires. **Test a knob with a value the other source could not produce, and read it back from
    `printenv` inside the container**, not from the function.
  - **A credential that is present is a credential that will be used.** A key that existed on an
    instance with no business using it was found polling that vendor's account — 45 calls against 12 —
    for positions it did not hold. An instance carries only the credentials of the venues it runs on,
    and the deploy precheck must not ask for others.
  - **A provider choice leaks.** Selecting a vendor for *data* built a client that *account-facing*
    code then used, because it gated on the data provider. Anything reading positions, activities or
    portfolio history is an execution question and follows the execution provider.
  - **Two instances sharing one vendor account share its rate limit**, and one of them loses: 3,516
    requests against a limit of 12 left an instance without a live bar for two and a half hours.

## 3. Check Nautilus first — every time

Before writing any order, execution or scheduling logic, **prove Nautilus does not already do it.** This
rule was broken three times, each costing more than the check would have: a hand-rolled bracket
(native: `OrderFactory.bracket()` + `OrderList`); hand-rolled scheduling with asyncio and a system
scheduler (native: `Clock.set_time_alert`, which also works in backtest); hand-rolled order-dependency
management whose cancel-and-replace stripped protection off five live positions (native:
`ContingencyType.OUO`, which modifies the linked order in place).

- **Read the config object's fields, not only its methods.** `StrategyConfig().dict()` lists everything.
  Constructing with `strategy_id` and `order_id_tag` alone left `manage_contingent_orders`,
  `manage_gtd_expiry` and `manage_stop` unset while their behaviour was reimplemented by hand.
- **The installed package is the source of truth** — not docs, not memory. `inspect.getsource` works on
  `.py`; for Cython read the `.pyx` in `site-packages` and use `__doc__` / `.dict()`.
- **Trace the chain to the adapter before concluding a mechanism helps.** A native feature that bottoms
  out in something the venue adapter cannot express is not a solution.
- **Review for conformance, not only for safety.** "Doesn't Nautilus already do this?" has been the
  right question every time it was asked.

## 4. Every bug gets a test

**A bug is not fixed until a test fails without the fix.** No exceptions for "obvious" one-liners.

- **Prove it.** Reintroduce the bug, watch the new test go red, restore, watch it go green — and say so
  in the PR. A test written after the fix that was never seen red is a guess about what it covers.
- **Test the seam, not the unit.** A passing test on a helper says nothing about whether anything calls
  it correctly. Five production defects shipped green in one day with correct units and wrong wiring: a
  float where Cython demands a `Quantity`; a scaling call deleted with no test noticing; a duplicate guard
  keyed on Python `id()` of a freshly built dict; a `NameError` in a projection that emptied the UI's
  book while eight positions were held; a filter on a flag the producer never set. Drive the real entry
  point — the projection, the command, the reconciler tick.
- **Build the double from what production actually emits, then check it.** Four of the five above hid
  behind a double that could not represent production — one accepted a float where Cython rejects it,
  one set a flag on an order type that never sets it. Print the real object's fields, or **bind the
  real method to the double, and make the double reject what production rejects.**
- **A double that cannot represent production is the bug.** Daily-bar fixtures stamped at the wrong
  hour let a fix pass and do nothing live. Fix the double; never loosen production to accommodate it.
- **Aim at the class, not the instance.** Ask what would have caught this *and its siblings*. A test
  that pins "no unscoped sticky offset anywhere" outlives one that pins "this tile is fixed".
- **Pin the reasoning, not only the value.** State in the test why the number is what it is — the input,
  the date, the P&L. An assertion that merely passes teaches nothing to whoever breaks it next.
- **Two derivations of one fact will disagree.** Where a check exists in two places, pin that they use
  the same predicate. This bit validation, derived state and a percent-to-bps rounding across the
  JS/Python seam.
- **Unverifiable by inspection → measure it.** Where behaviour depends on a venue, write a probe and
  record the number. Guessing at venue semantics is what produces the defects that look like features.
- **Verification by disagreement beats verification by inspection — in both directions.** *Identical
  when it should differ* is a dead mechanism (flags computed and discarded, returning byte-identical
  results). *Differing when it should match* is a live defect (two implementations of one simulator
  disagreeing by 1.69% exposed a population bias no review had found). Across a dozen measured defects,
  code review caught none; two implementations disagreeing, arithmetic that could not be true, and
  peers re-checking settled claims caught all of them.
- **A test that cannot fail carries no information — prove the fixture makes the bug reachable.** An
  invariance test passed twice with the look-ahead deliberately reintroduced, because the fixture never
  approached the threshold that would have made the gate bind. **Assert the fixture's own property first,
  then the invariance.**

## 5. Agreement is not connection

**Two sources agreeing is not evidence they are connected. It is the exact condition under which a
severed connection is invisible.** Eight instances in one day, every one green, every one measuring
something adjacent to what mattered: a settings knob whose value equalled the hardcoded default it never
replaced; a risk-limits object declaring ten fields of which the runner read one; two lanes with
different universes emitting byte-identical orders because the harness ran one lane's gateway for both;
a runbook stating a safety property exactly inverted from the code.

- **Test a knob with a value the default could not produce.** Never 20 000 when the constant is 20 000.
  Pick 12 345 and watch it travel.
- **When a bite does not bite, ask whether you cut every wire.** An insufficient mutation and an unearned
  test are indistinguishable from the green.
- **When a mutation kills its neighbours and spares its target, the target is the suspect.** That is the
  only reliable outside signature of a vacuous test.
- **Enumerate the siblings.** When checking one entity's configuration, check every other entity on the
  same account and code path.
- **Prose that reads as safety is the most dangerous kind of wrong**, because it survives review by
  sounding careful. Verify prose about another repository against that repository's source.
- **Absence of evidence is a timestamp, not a property.** An empty journal or a lane that has never
  decided says nothing about what *can* happen.

## 6. A fallback is not risk reduction — it is a silent wrong answer

Every defect below was a fallback doing exactly what it was written to do. None raised. All were found by
reading an artifact, never by a failure.

- **`or` cannot tell "unset" from "zero".** `allocated or broker.equity()` sized a lane off the whole
  account instead of its sleeve; the lane never placed an order in its life while every surface said
  TRADING. Use `x if x is not None else y`, and prefer refusing to guessing.
- **A default argument is what makes a missing argument invisible.** A helper that supplied a default and
  also forwarded `**kwargs` raised on the one caller that passed the value explicitly — killing two lanes
  at the decision row on a stack where every lane read RUNNING.
- **A swallowed exception on every poll is indistinguishable from a clean poll.** Two detectors were inert
  for two deploys because their sender was called with the wrong type inside an `except Exception`. A
  check that fails must eventually report *itself*, or the fail-soft is just off.
- **A fallback branch nobody can reach is worse than no branch.** It looks like coverage.
- **Zero-value defaults are the same trap in data.** A sleeve defaulting to 0 with nothing wiring the
  seeding made `deployable` 0: armed, TRADING, correct in every log line, unable to place an order.

**The rule.** Where a value cannot be established, **raise or refuse, and say which input was missing.**
Reserve fallbacks for cases where the fallback is genuinely as correct as the primary — and say so in a
comment, because the next reader cannot tell a considered fallback from a lazy one. When you must
degrade, degrade *loudly*: report the degraded state as its own condition, never as the healthy one.
**Three states beat two** — "absent" is not "false", "not asked" is not "not protected", "0 of 0" is
not "0 of 4".

## 7. Absence must not be readable as permission

The same defect arrived five times in one day wearing different clothes: `or` reading a real 0 as unset;
`nan <= 0` disarming a halt because comparisons with NaN are false; an empty map invisible to the detector
written to catch it; a venue rate limiter that refuses by returning an **empty array and no error**
(1 516 empty responses read as a venue with no data); a day the venue never described, read as a trading
day because the calendar window is shorter than the look-ahead.

**Three states, never two.** Known-yes, known-no, never-told-us. The third is its own answer — a typed
refusal rather than `None`, a REFUSAL rather than a default, "0 of 3" rather than silence.

- **A detector that recognises its subject by a property the defect destroys can never fire.** A guard for
  "a stale copy of the instrument ids" looked for containers holding instrument ids; an empty dict holds
  none. An `exists()` probe on a watchdog key is satisfied by the corpse of the last healthy boot. *Empty
  is the bug, so the bug cannot be what identifies it.*
- **A detector cannot signal by raising through a fallback built to absorb raises.** A tripwire that
  raised from a fake connection was swallowed by the `except Exception` that exists so a database hiccup
  cannot stop a boot; three tests stayed green while the read they vouched for happened on every one.
  *Assert on the record of the attempt, not on an exception escaping* — and aim the vacuity guard at the
  level the fallback lives at.
- **"It cannot be done" is a measurement, not an opinion.** Three capability claims in one day were
  stated as checked and were wrong within ten minutes of someone looking — a per-lane window P&L "not
  derivable" while the lot matcher that derives it already existed; a missed capture "permanently gone"
  on a venue whose durable cache is retained forever. *Run it, or say "not checked".* The cost of being
  wrong is a ticket filed against a false premise that then shapes every decision on top of it.
- **When identifying a row without storing a conclusion, reach for provenance.** A `capture_kind` written
  by the job that fired the capture is a fact about which job wrote the row — neither a read-time rule
  that can drift nor a stored verdict that cannot be re-derived.
- **Enumerating the callers of a hazard is what keeps being wrong — enforce it at the value.** Eight tests
  reading a live database were found in four rounds, each round ending with the class declared closed;
  the scan that found them was evadable in the way its own error message taught. The fix was to delete
  the scan and seed the cache so the read is unreachable unless asked for. *When a rule needs a list of
  everywhere it applies, make the dangerous thing unreachable instead of keeping the list correct.*
- **Non-empty is not complete.** A sweep that found *something* did not find *everything*: a wiring
  check matching one AST shape found three call sites and missed both production lanes. Assert
  coverage — every module importing the thing yields at least one resolved use.
- **A change that anchors on something absent does nothing, quietly.** A method defined and never called;
  an edit anchored on a field that existed only on an unmerged branch, so every replace no-op'd; a keyword
  passed to a constructor that never accepted it. *Assert the anchor was found*, the same way you assert
  a mutation bit.
- **Lazy is not always available — check when, not only where.** A symbol can resolve at `on_start`
  because subscription follows connect; a claim cannot, because reconciliation precedes it and claims
  exist to catch reconciliation-generated orders. Registering afterwards protects against an event
  already booked — while looking like it worked. Kill options with kernel ordering, not opinion.

## 8. Observation is a mechanism, not a try/except

Anything that watches the system runs on a live path and must not be able to break it — and must not be
able to fail silently either. Those two requirements are **one mechanism** (`api/observation.py`), not a
`try/except` at each site. The per-site version was written three times in one file in one day, and one
had an except branch that raised — on any object without a clock it aborted the entire protection pass,
which on a live stack would have read as a protection outage rather than a broken probe.

```python
observations.declare("book_truth", "feed_stale")           # at wiring time
observations.run("book_truth", compute, reports, ts_ns=now)  # one code path; absorbing without recording is not expressible
```

- `Exception` only, never `BaseException` — interrupts, exits and cancellations are the runtime asking to
  stop.
- The recorder is itself guarded, touches only built-ins, counts its own failures, and takes the
  timestamp as an argument — depending on anything inside an error handler is the defect this replaced.
- `declare()` makes absence readable: `ok`, `failing`, `never ran` are three states. On a failures-only
  surface an observer that produced nothing looks exactly like a healthy quiet one.
- Failures coalesce into a count; drops past the registry cap are counted. A defence that hides what it
  discarded is a second bug.
- Rendering is separate from recording, so a broken log sink cannot lose the record.

What it does not do: nothing prevents a bare `except Exception` beside it (that guard is review); it
wraps synchronous callables only; it is not a logger and must not become one.

## 9. The defect procedure

Every defect goes through this, in order. No step is optional and none may be reordered.

1. **Ticket.** What was measured, where, what it cost — numbers from the running stack.
2. **A failing test that proves it.** Seen red, for the right reason, before any fix exists. A
   fixture-property assertion first.
3. **Review the test coverage** — not the fix, which does not exist yet. Does it cover the surrounding
   cases and the sibling paths?
4. **Scope the fix.** What changes and what deliberately does not.
5. **Review the scope.** The cheapest place to find out the plan is wrong.
6. **Implement.**
7. **Review the implementation.**
8. **PR. Merge.**
9. **Review the merged result.** What landed is not always what was reviewed.
10. **Deploy.**
11. **Monitor on live.** State beforehand which number must move and which must not, then read it back
    off the running stack. A deploy that is not observed is a claim, not a result.

Reviewing a finished change asks "is this right?", which invites agreement. Reviewing the test asks
"what else breaks this?" and reviewing the scope asks "is this the right change?" — different questions
find different things. One fix took four review rounds and each found a distinct defect, including one
where the fix had reintroduced the bug it was for.

**The reviewer is a budget and a provenance.** One review per procedure step, never per iteration —
findings folded, not re-reviewed. When the primary reviewer is unavailable, a **named cross-review by a
different reader with different assumptions** is the substitute; a same-model pass with the same prompt
is an additional pass, not that. Every PR states which reviewer it got, by name.

**The merge gate is a script, not a hosted CI.** `backend/scripts/merge_gate.py` runs the suite green
against the editable strategies tree *and* against the pinned strategies ref in a throwaway checkout, and
the pinned run's anchor is asserted *inside* the suite (`conftest.py` refuses the session unless the
strategies package was imported from the expected tree) — because a separate interpreter invocation
proves nothing about what the tests imported. It does not guarantee the image; the deploy verify reads
the running container for that.

## 10. A full status analysis of a running instance means this

A status read that stops at `/health.status` is not one. The first full pass on a staging instance,
36 minutes after the open, found seventeen orphan stops on dead client ids, a 190-per-minute warning
storm, a refetch churn starving the compass, a stranded claim and a protection-flag disagreement —
behind a `/health` that said `degraded` for one of them.

Every item, every time, numbers not adjectives. Absence of a row is "not asked", never "clean".

1. **Logs since the open, classified**: engine, api, gateway. Counts per level; distinct message classes
   (strip ids/symbols/numbers, `sort | uniq -c`); rate per minute for the top class; first occurrence
   since boot. A warning repeated 6 834 times is one defect, not noise.
2. **Venue truth, read from the venue**: open orders at the venue vs `/orders`; positions at the venue vs
   cache vs claims. Every order the cache cannot name, listed with its client id. Every claim without a
   position, listed.
3. **`/health`, every field**: status *and why*; subsystems; feed tick age; subscriptions
   requested/bound/silent with the silent list read; unpriced positions; reconcile drift; split and
   protection divergence; book truth; observations declared/ok/failing/never-ran; armed lanes; realized
   legs.
4. **The book, per held name**: side, qty, avg, last-price age in seconds, unrealized, engine-side
   protection vs the venue's resting stop (two derivations — they must agree), bar subscriptions bound.
5. **Lanes**: lifecycle rows (absent = TRADING), next fire per lane, today's journal rows per lane, pool
   freshness against its gate, sleeves (target/actual/deployed), what the next slot will do and why.
6. **Storage and process**: Redis persistence status and memory, database connections, container
   CPU/memory, API latency, UI HTTP 200.
7. **Gateway**: connection, login and farm lines since boot; second-login risk stated.
8. **Refetch/heal churn**: never-completed lines per minute and the ids they name.
9. **Output**: one block per defect — What · Cause · Impact now · Impact if ignored · Fix · When · Need
   from you — each filed as a ticket with the measured numbers *before* the summary is written, and a
   go/no-go per state change. Reads that changed nothing are one line each.

## 11. Conventions

- Conventional Commits. Tests beside source. Comments in English.
- Every non-hidden directory carries a README: what it holds, what does not go there.
- Vertical slice before breadth: one ticker, one order, one paper account end to end before adding
  lanes or tiles.
- **Prose beside a value is part of the value.** When changing a configuration value, change the
  sentence above it in the same commit. A comment that reads as a safety property while the value
  beside it says the opposite survives review by sounding careful.
- **Commit bodies are the decision record.** Authorship proves nothing about who decided; read the body.
