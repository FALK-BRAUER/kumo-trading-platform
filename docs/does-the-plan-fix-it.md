# Does ADR 0002 fix the current situation?

> Operator, 2026-08-19: *"You are supposed to review the plan that it fixes the current situation in a
> principled way."*
>
> That test had not been applied. Three reviews checked whether the plan is internally coherent and
> whether its claims survive source. None asked whether it repairs what is actually wrong today. This
> does, item by item, against the live system.

## The verdict first

**The plan fixes 3 of the 11 things that are wrong right now. Of the eight it does not fix, six are not
mentioned in it at all.** The single most damaging failure of the past week — five lost trading sessions —
was fixed by a one-line signature change that is independent of the entire architecture.

That is not an argument against the architecture. It is an argument that **the architecture is not the
work in front of us**, and the plan has been presented as though it were.

## Item by item

| # | What is wrong RIGHT NOW | Does ADR 0002 fix it? | What actually fixes it |
|---|---|---|---|
| 1 | **No rotation for 5 sessions** (08-14 → today) | **No** | Already fixed — the `slot` kwarg, one line, shipped. Independent of the seam |
| 2 | ~~Every MOMENTUM-002 entry is refused~~ **NOT A DEFECT — this is the wind-down working.** MOMENTUM is over target and is *supposed* to reduce | n/a | Nothing. It is correct behaviour and it is already implemented |
| 3 | **`allocated_equity` has no producer**, so sizing runs off the full ~$100k account: ~$10k a name against a $20k target built for eight | **No — not mentioned** | Wire it. One assignment. It is the *cause* of #2 and of the 47k overflow |
| 4 | **MOMENTUM-002 at 67,111 against a 20,000 target** | **No — and the ADR's design here should be DELETED** | Already handled: entries refused + exits allowed = reduce-by-attrition, paced by the strategy's own exit rate. The real gap is `TRANSFER_TO = ''`, so freed capital parks in UNALLOCATED instead of funding BCTROT. One setting |
| 5 | **VCTR held under `{"ok": true}`** — an exit that failed and nothing learned | **No** | Outcome feedback (ks#51). Correct under *any* boundary — it does not need the seam |
| 6 | **AEM: three quantities for one position** (18 cache / 2 store / 0 venue) | **Partly** — Law 1 names the class; nothing in the plan retires the stale claim | The claim-retirement path, which has not run since 08-14 because sessions were dead |
| 7 | **Operator flatten refused, stop already cancelled** (CGAU) | **Yes** — one controller, shared authority, TTL-bounded suppression | The plan, and only the plan |
| 8 | **Protection is the only active control** on 7 positions | **Yes, indirectly** — it is what the plan is careful never to break | Nothing needs changing; it works |
| 9 | **`stop_reenter_rearm` and `pyramid_watch` have never run** — zero rows, ever | **No** | Diagnose why the handoff never fires. Independent of the seam |
| 10 | **Both price-staleness guards are dead** — `last_price()` reads no timestamp | **No** | Enhancement A2. And the plan makes it *worse*: under platform actuation a stale-price entry becomes a platform action |
| 11 | **A failed session is invisible** — found by a human on day three | **No** | Enhancement A4 (#349, #199). The transport is built and called by nothing |

**Fixed by the plan: 7, 8, and half of 4 and 6.**

## What that means, principled rather than defensive

**1. The architecture is a prevention, not a repair.** ADR 0002 makes a *class* of failure structurally
impossible — two controllers over one resource, an action with no observed outcome, a contract that can
drift unchecked. Every one of those is real and each has cost money. But almost nothing currently broken
is broken *because* the architecture is wrong. It is broken because features were built and never wired.

**2. The dominant failure mode in this system is not the one the plan addresses.** The independent
catalogue found ten categories across 95 incidents. The largest untouched one is **"built, configured,
deployed, never executed" — 14 incidents, never a fix campaign.** Items 2, 3, 9 and 10 above are all that
category. So is the `_forced_exits` precedent the plan cites to justify its own wind-down, which has
returned zero symbols ever. So is `LIQUIDATING`, never entered once.

**A plan whose safety argument rests on mechanisms that have never executed is not a principled fix for a
system whose main problem is mechanisms that never execute.**

**3. The sequencing is inverted.** The plan's first step is a migration; the enhancement list's section A
is seven items of live exposure that need no architecture at all. Five of the eleven rows above are fixed
by section A. None of them requires the seam to move, and several — `allocated_equity`, staleness —
should be fixed *before* any platform starts actuating on their output.

## What a principled response looks like

**First, and today:** decide #2 — the entry block — before 13:35Z. It is a capital decision, not a
technical one.

**Then, in this order, none of which needs ADR 0002:**

1. `allocated_equity` wired (#3) — one assignment, and it is the root of #2 and #4
2. Outcome feedback (#5) — correct under any boundary
3. Price staleness (#10) — a prerequisite for anything that actuates automatically
4. Session-failure alerting (#11) — so the next dead week is found in hours
5. `_submit` passes `position_id`; coverage subtracts reservations
6. Diagnose the never-fired manager chains (#9)

**Then, and only then, ADR 0002** — for the class of failure that genuinely needs an architecture, on a
system whose current wounds are already closed.

## The honest summary

The plan is good work aimed slightly past the problem. It should not be abandoned — items 7 and 8 are
real and the invariants have already caught defects that nothing else caught. But it should not be
implemented first, and it should stop being presented as the answer to "what is wrong right now".


---

## Correction, 2026-08-19 — the wind-down needs no new mechanism, and mine should be deleted

Operator: *"momentum is supposed to reduce."* Correct, and it invalidates a design in ADR 0002 that I built.

`budget_gate.py:113-116`, verbatim:

> *"Exits are ALWAYS allowed. A strategy over its budget needs to sell; blocking that would trap it above
> its target forever."*

So the reduction mechanism is **entries refused, exits allowed — reduce by attrition**, drained on fills
by the sleeve transfer. It is paced by the strategy's own exit rate, which is exactly the *"over N
sessions, worst-ranked first"* that #350/ks#46 asked for, achieved with no new mechanism at all.

**Three things I got wrong follow from that:**

1. **"`must_reduce` has no actor" is false.** It has one — `budget_gate` plus the fill-driven transfer —
   and it works. #350 and ks#46 should be re-scoped, not built.
2. **The paced-EXIT wind-down designed in ADR 0002 should be deleted.** One name per session, platform
   issues `EXIT` by the strategy's ranking, bounded by turnover — all of it unnecessary, and worse than
   unnecessary: it would add a second mechanism selling positions alongside the strategy's own exits.
   That is the two-controllers-over-one-resource pattern this ADR exists to remove, which I would have
   introduced in the section arguing against it.
3. **Today's session is not a problem.** The first rotation since 08-14 running exits-only is the
   wind-down finally executing after five dead sessions. Nothing needs deciding before 13:35Z.

**What remains genuinely broken in this area, and it is small:**

- **`TRANSFER_TO = ''`** — freed capital returns to UNALLOCATED rather than funding BCTROT-004, so the
  handover the settings text describes (*"takes over gradually as MOMENTUM sheds positions"*) cannot
  happen. One setting.
- **`allocated_equity` has no producer** — the cause of the 67k overflow in the first place, and it will
  produce the same overflow for any strategy that is ever funded. One assignment.

Reduce-by-attrition also answers a question the ADR spent a long time on: the wind-down does not need to
choose *which* names to sell, because the strategy's own exits choose them. The platform only has to stop
funding new ones. That is a strictly smaller responsibility than the one I gave it.
