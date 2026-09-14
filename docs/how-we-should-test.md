# How kumo-cockpit should be tested

Operator, 2026-08-24: *"sort out how to test. too many bugs every day. Maybe we need a cockpit harness
around the BacktestEngine. I'm not sure."*

Three of us scored this independently against defects we ACTUALLY shipped — not against which approach
sounds most rigorous. We converged: **(b) first, (a) last**, for reasons none of us held at the start.

## The measurement that decides it

From qs6ptueg, taken before arguing:

    PgSessionRunner.run()      0 end-to-end drives      <- every lane, both stacks, goes through this
    QC27SessionRunner.run()    3
    Broker.position_entries    0 references anywhere

`PgSessionRunner.run()` carried tonight's `TypeError`. Its tests are `@pg`-marked, so they are among
the ten skipped in every local and CI run. **`make dryrun` against a database clone is the only thing
that has ever executed it.** That is not a discipline gap — that file has been mutation-bitten more
than any other — it is a gap in what is EXECUTABLE, and care does not substitute for execution.

## The corpus

Thirty-odd defects across three sessions. Sorted by HOW they were found (ni9q2dnz):

| found by | defects |
|---|---|
| two independent derivations disagreed | decision-row TypeError · account plane · realized-vs-broker · trailing stop stale by $447.20 · the 28% equity understatement |
| an artifact was read after a reported success | disarmed gates · stale `versions.lock` · two detectors shipped inert · zsh `:e` collapsing three tags into one · a `checkout -b \|\| checkout -b` that left a detached HEAD |
| a mutation was applied | a dead `if self._http is None` guard · four of six lying doubles |
| the real thing was driven with a strict double | the TypeError · unexitable held names |
| a static property was checked | five mechanisms with no caller · the kwarg collision itself |

**Not one was found by simulating a market.** Every one came from comparing something against
something else, or from executing a path nobody had executed.

**Nine of eighteen on the strategies side alone are "existed, never executed on the path that
matters."** That is the disease.

## Why (a) ranks last — three separate reasons

**1. It scores ~0.5 out of 8** on the cockpit corpus. The session runner's journal write touches
Postgres, so a backtest that stubs the journal never reaches it. Deploy-time, config-time and static
defects are invisible to any simulator.

**2. The harness would be the largest double we own** (ni9q2dnz). A simulated venue, clock, fill model
and account, added to a codebase whose dominant failure mode this week was *a double that could not
represent production* — six of them, plus five more in my own dry-run harness. It moves us toward the
failure mode, not away.

**3. It is the easiest place to build something that looks rigorous and tests the framework instead of
us** (qs6ptueg, from tonight). Their first four reduce-only tests drove a real `BacktestEngine` with a
real `RiskEngine`. All four passed. **They reverted the fix and all four stayed green** — the tests
entered through `Strategy.submit_order` directly, proving Nautilus's behaviour and saying nothing about
whether our code triggers it.

> If (a) is ever built, its own conformance rule must be: **every harness test enters through a
> production entry point**, never a Nautilus API directly. Without that it generates confidence faster
> than coverage, which is worse than not having it.

## Where I was wrong, and what (a) IS uniquely for

I proposed its unique catch was strategy logic against fills — partial fills, gaps, the give-back
trail. **Wrong.** Not one defect in the combined corpus is a strategy-logic defect, and that surface is
already covered by the pandas backtests and the trade-for-trade diff.

Its real unique power is **executing Nautilus's contract**. The `reduce_only` finding is the proof: the
venue fact ("IB ignores it") is catalogue-shaped, but the actual cause — *Nautilus's RiskEngine skips
the check entirely when `position_id` is absent* — is a FRAMEWORK behaviour, invisible in source
because `risk/engine.pyx` is Cython, with the flag set correctly on our order and every static and
contract check passing. It was found by watching a real `RiskEngine` deny one order and accept an
identical one.

So a harness is worth building — **scoped to framework contract**: order denial, NETTING attribution,
`positions_open(strategy_id=…)` after reconciliation, what survives a restart. Maybe a dozen tests.

## Why (d) is necessary and not sufficient

Contract tests catch **shape**, not **semantics**. `reduce_only` would pass every contract test that
could be written: the signature accepts it, the flag is set, the order is well-formed, both repos
agree — and it is simply not honoured. Same class as a parameter accepted and discarded, and a member
required and never called. **Three defects in one class that static agreement cannot reach.**

## (e) — the thing none of us proposed, in two halves

**Seam execution coverage** (qs6ptueg). Not line coverage: *every function on a declared seam must be
reached, at least once, by a test that arrives through its PRODUCTION caller.*
`PgSessionRunner.run: 0 drives` and `position_entries: 0 references` are exactly what that metric
reports, and both are real today.

**Post-deploy conformance** (ni9q2dnz). Every check is two derivations of one fact, failing on
disagreement:

    declared lanes        vs  strategies RUNNING BY NAME in the log
    planned refs          vs  the containers' own digests
    declared config       vs  `printenv` INSIDE the container
    engine positions      vs  the venue's
    engine equity         vs  the venue's
    declared mechanisms   vs  their callers

Seconds to run, no simulator, and **the only instrument on this list that would have caught tonight's
disarmed gates** — which otherwise cost a two-lane stack until someone noticed nothing happening.

## Recommendation, in cost order

| | what | why |
|---|---|---|
| 1 | **`make dryrun` promoted to a pre-open GATE** | built; found the worst defect of the week; the only thing that has ever executed `PgSessionRunner.run` |
| 2 | **post-deploy conformance + seam execution coverage** | seconds, no infra; catches the config/deploy/orphan classes outright |
| 3 | **shared contract suite in both CIs** | catches the boundary class cheaply |
| 4 | **venue catalogue (#509)** | stops the next double inventing a venue |
| 5 | **Nautilus harness, framework-contract scoped** | a dozen tests, entering through production entry points only |

## The honest caveat

The corpus is **defects we found**. It selects for what our current instruments can see. A backtest
harness may be catching nothing because nothing is looking for strategy-logic errors — and a strategy
subtly wrong about its own signal would appear nowhere in this list. That is a real gap and it is the
argument for (a) **on its own merits, after the platform stops dropping orders** — not as a cure for
the current disease.

## The one-line version

The bugs are not coming from insufficient simulation. They come from code that has never been run, and
from tools that report success without producing the result. **Execute first; simulate later.**
