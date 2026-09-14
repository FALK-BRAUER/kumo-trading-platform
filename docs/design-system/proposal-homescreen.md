# Proposal: the homescreen

**Status: OPTION A CHOSEN (2026-08-11). AI approved into the UX. Nothing here is built yet.**
Mocks: `mock-homescreen.html` (the three options side by side) and **`mock-homescreen-a.html`**
(Option A drawn in detail against real state, 2026-08-11 ~19:05 SGT). Both standalone — nothing in
`ui/` touched, neither merged into `mock-v2.html`.
Research: `research/homescreen.md`, `research/ai-analysis.md`. Data-path epic: GH #212.

> **the operator's decisions, 2026-08-11**
> 1. **Option A** — the status board, not the triage screen recommended in §4 below. §4 is left
>    unedited as the record of what was recommended and not taken.
> 2. **AI is in the UX.** Scoped in §8 to the journal-grounded briefs. Market and Radar remain open.
>
> This supersedes §4's recommendation. §§1–3 and 5–7 still hold — the data contract in §5 in
> particular is what makes §9's scorecard uncomfortable.

Requested by the operator as one of three topics (homescreen · Alpaca charting · embedded AI). This is the
scope + UX for the first, drawn against real state from 2026-08-10.

---

## 0. Disclosure, because it affects how you read this

A `Strategy` tile and a `Home` view are **already deployed** to paper (PR #222). That was built ahead
of any signed-off scope — my error, not a decision you made. It is read-only and reversible: removing
the view is one line in `layouts.ts`.

**Recommendation: treat it as a fourth, already-visible option** rather than as the answer. It is
closest to Option B below and shares its data path, so keeping or pulling it does not change any of
the choices here. Say which and I will act; I have left it in place rather than making a second
unilateral change.

## 1. The question the screen answers

> **Can I leave this alone, or do I need to act?**

Everything earns its place by helping answer that. The corollary is the design constraint that
matters: **on a normal day the screen should be boring**. If it is healthy it should say so in one
line and let you leave.

That is deliberately not "how am I doing" — the Portfolio tab answers that, and answering it here
would make the landing surface a P&L mirror you check for pleasure.

## 2. Why kumo-trader's HomeDashboard is not the template

`kumo-trader/ui/components/HomeDashboard.tsx`, 682 lines, is a finished answer to a **different**
question — and it is worth reading before deciding.

| block | content |
|---|---|
| AI brief | streamed bullets, parsed into labelled rows (MARKET / MOVING / DO / WATCH OUT / RADAR) |
| P&L | realized · secured · unrealized · net, plus per-position entry/live/stop/cushion |
| Sectors | per-category rollup, `+++` count over total, avg score, 8-block bar |
| Actions | priority-ranked high/medium/low |
| Calendar | earnings with `daysUntil`, flagged if held |
| Risk | maxLoss, secured, deployed, sector concentration, **edge**: trades, winRate, expectancy, avgWin, avgLoss |

**Worth porting:** `secured` (the gain locked in because the stop sits above entry — we have no
equivalent concept) and the market-phase awareness.

**Not worth porting:** indices, sector heat, RADAR picks. That design existed to help a human *find*
trades. This cockpit supervises a strategy that has already ranked and decided; a second, incompatible
ranking on the landing screen is an invitation to override the system you built to stop yourself
overriding. Today's data supports that: the pool's beaten-down cohort was the *worst* group
(n=9, +0.57% against a pool mean of +0.97%) while containing both the biggest winners and losers.

**Deferred, with a reason:** the edge block (win rate, expectancy). We have ~130 in-sample sessions
and **no holdout** — every parameter was chosen across all of it. A win-rate figure on the landing
screen would manufacture confidence the data cannot support. It belongs in research until there is an
out-of-sample window.

## 3. The three options

Rendered side by side in `mock-homescreen.html`. They are alternatives, not stages.

### A — Status board
Everything on one screen: account hero, strategy/manual split, attention list, session summary, top
positions. Closest to what you already lived with.

*For:* one glance gives position, performance and problems. Familiar.
*Against:* densest; the P&L is the loudest thing on it, which trains you to open it for the number
rather than for the signal. Most likely to be read daily out of habit.

### B — Triage *(recommended)*
One verdict line, then only exceptions. P&L present but demoted to a three-cell strip.

*For:* directly answers the question in §1. On a clean day it reads "Nothing needs you" and you
close it. Cheapest to build — it is roughly what is already deployed.
*Against:* if you *want* a daily performance surface, this is not it, and you will end up opening
Portfolio anyway.

The mock draws three states deliberately: a normal day with two flags, a clean day, and — the one it
must never get wrong — **engine silent for 14 min**. A frozen frame is not a calm one, and the screen
has to say so rather than keep rendering the last good state.

### C — Phase-aware
Option B plus reordering by market phase: pre-open shows readiness, after close shows reconciliation.

*For:* the useful question genuinely does change. Before the open it is "will it trade correctly";
after the close it is "did it, and what did it cost".
*Against:* four layouts to build, four to keep correct, and a phase boundary that will be wrong at
least once (holidays, half-days, a DST edge). Only worth it if the phases feel different to you in
practice.

## 4. Recommendation

**B now, C later if the phases prove to matter.** Specifically:

1. Ship B's verdict + attention + strip. This is largely built.
2. Add the **strategy/manual P&L split** — the single highest-value number missing today, because the
   current header conflates them (the "+$501" that was mostly FIG, while the automated book was +$210).
3. Add **staleness**, which B already has and which is the only genuine safety item on the page.
4. Revisit `secured` once we decide whether MOMENTUM ever carries resting stops. It may not translate:
   our exits are rule-based, evaluated per session, not orders sitting at the venue.

## 5. Data contract — what exists, what does not

Verified against the running system (`research/homescreen.md` §5).

| need | source | state |
|---|---|---|
| Lifecycle, decision, reasons, journal, trail | `session` frame | **exists** (#212, deployed) |
| Equity, day P&L, cash, positions | `account` + `positions` | **exists** |
| Strategy/manual split | `positions[].strategy_id` | **exists**, not surfaced |
| Market phase, next session time | Alpaca `/v2/clock` | **called by the alerts loop, not exposed** |
| Staleness | frame `ts` | **exists**, needs the client to compare |
| `secured` | — | **does not exist**; needs a stop concept we may not have |
| Edge stats | closed cycles | exists but deferred, see §2 |

So B needs **one** new thing: market phase exposed to the UI. C needs that plus phase-specific layouts.

## 6. Explicitly out of scope

Charts · the full position table · top movers or indices · any control that submits, cancels, pauses or
arms · the AI panel (separate topic; see `research/ai-analysis.md` — the recommendation there is that
it summarises *our own journal*, which it can be checked against, not the market).

## 7. Questions for you (answered where the operator has ruled)

1. **A, B or C** — and does the landing tab change at all, or does Home sit beside Portfolio without
   becoming the default?
2. **Keep or pull the already-deployed tile** while this is decided?
3. Is a **daily performance surface** something you want somewhere? If yes it argues for A, or for
   Portfolio growing a summary rather than Home absorbing one.
4. Does `secured` survive without resting stops, or is it a kumo-trader concept that does not
   translate?
5. Phase-aware: do pre-open and after-close genuinely feel like different jobs to you, or is that my
   inference from the codex review?

**Answered:** Q1 — **A**, landing tab still open. Q3 — implied yes by choosing A. Q2, Q4, Q5 still open.

---

## 8. What re-reading kumo-trader's HomeDashboard changed

`kumo-trader/ui/components/HomeDashboard.tsx:642-681`. §2 above tabulated its blocks but not their
prominence, and prominence is the finding.

```
PnlCard                        ← full width, top
Market · Portfolio · Radar     ← AI briefs, 3-col
P&L Analysis                   ← AI brief, full width
Calendar · Risk                ← 2-col
Sectors · Actions              ← 2-col
```

**Four of the eight blocks are `BriefSection` — streamed AI — occupying the slot directly under P&L.**
The home the operator lived with was substantially an AI surface. Choosing A therefore decides the AI question
whether or not anyone says so out loud, which is why it was put to him explicitly rather than inferred.

### Which AI blocks, and why not all four

`research/ai-analysis.md` §0 draws the line:

> Deterministic code computes facts, signals, risk, orders, exits. The model **explains, summarizes,
> questions, and audits** those facts. If it cannot cite the data behind a claim, it does not say it.

| Brief | Verdict |
|---|---|
| **Portfolio** | **In** — summarises our own book; every claim traceable to a bundle field |
| **P&L Analysis** | **In** — explains our own results; same |
| **Market** | **Open** — market-facing. kumo-trader ran it on Gemini with Google Search grounding. Nothing here can verify a market claim, so it would be the one block on the page that cannot be checked |
| **Radar** | **Open, recommend dropping** — §2 already argues it: MOMENTUM has ranked and decided; a second incompatible ranking "is an invitation to override the system you built to stop yourself overriding" |

Both in-scope briefs run through `backend/api/narrative/check.py`, which already exists and was
deliberately written before any model (#213). `mock-homescreen-a.html` draws them with a per-claim
citation chip and a footer pass count.

Worth noticing: **the AI blocks end up the only ones on the page that show their work.** No other block
cites a source. That inverts the usual objection to AI on a dashboard — here the model output is the
most auditable content, because it is the only content required to cite.

### It polls, and we do not

`fetchAll` runs on a 5-minute `setInterval` across five REST endpoints
(`/api/home/{pnl,sectors,actions,calendar,risk}`) plus four streaming brief endpoints. `CLAUDE.md` for
this cockpit: *"Data feed → UI over WebSocket. No polling."*

So **A is not a component port.** Each block becomes a bus-driven plane. That is a data-layer build,
and it lands directly on the architecture question in §10.

## 9. Scorecard — what Option A actually costs

| Block | State |
|---|---|
| P&L (realized · unrealized · cash · strategy/manual split) | **full data path** |
| Actions | **full data path** — attention list off the `session` frame |
| Risk (deployed · cash · buying power) | **full data path** |
| AI · Portfolio | needs a model wired behind the existing checker |
| AI · P&L Analysis | same |
| Calendar | **no data path** — no earnings feed in the cockpit |
| Sectors | **no data path** — kumo-trader's rollup ran on its own scoring system; we have no scores. Portfolio-wide sector exposure is a different thing and lives in #12 / #80 |
| `secured` | may not translate — needs a resting stop above entry; MOMENTUM exits are rule-based per session |
| Risk · edge stats | deferred — ~130 in-sample sessions, no holdout (§2) |

Three of eight ship on data we have today. This is the same shape of finding #212 made about the
original six blocks, with one difference: this time it is known before anything is built.

## 10. Architecture — Option A compounds an existing drift

`CLAUDE.md` (decided 2026-07-01): *"UI-data abstraction = Nautilus MessageBus, not hand-built … the bus
carries pure Nautilus domain objects and `msgbus.serializer` **IS** the DTO/serialization layer."*
It names Option A (a display `Actor` forwards bus objects to the WS) and Option B (`MessageBusConfig`
with `database=redis`, `stream_per_topic`).

What the code does — `engine_node.py:_publish()`, the single funnel every UI plane goes through
including `session` (`:687`):

```
_publish(kind, payload) → json.dumps(payload) → queue.Queue → writer thread → Redis XADD/SET
```

A hand-rolled DTO down a private pipe. Total `msgbus` usage in the backend is five call sites, all
consume-side or unrelated: three `subscribe`, one fundamentals `publish`, one `send` to
`ExecEngine.process`. So the bus is read correctly and then bypassed on the way out — neither Option A
nor Option B, but an undocumented third path.

Fair caveat: `session` is **not** a Nautilus domain object. Lifecycle, decision reasons, journal rows
and exit trail are our concepts; `msgbus.serializer` was never going to emit them. The rule fits
`Bar`/`QuoteTick`/order/position cleanly and fits custom planes badly, so part of this is the decision
being under-specified rather than simply ignored. Precedent that it is fixable: `1022b4b` —
*"publish onto Nautilus's own message bus, not just Redis"* — already corrected fundamentals.

**Why it matters here:** homescreen Option A adds roughly six new planes. Building them through the
private pipe triples down on the drift. Decide the seam before, not after.
