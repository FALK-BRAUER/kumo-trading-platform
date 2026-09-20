# The protection slot — one mechanism for every protective order

Design for kumo-trading-platform issue 269. Written 2026-08-17 after the ninth defect in this class.

Subsumes #245 (PEAK trims race their own cancel) and #254 (arming PEAK cancels the bracket stop
before the manager rearms). Both are instances; neither needs its own fix once this exists.

---

## Why this keeps coming back

**There is no chokepoint.** PEAK, the #239 backstop, bracket legs, PYRAMID, MOMENTUM's exits and
manual flatten each call `submit` / `cancel` on protective orders directly. Every past fix patched
one caller's cancel path, so the next actor added reintroduces the class.

**The contended resource is invisible in our model.** Alpaca reserves shares against *any* resting
sell, so a position backs exactly **one** resting exit. That rule is written nowhere; each manager
rediscovers it by being rejected. the operator's live NBIS today: `qty=29, qty_available=0`, PEAK's own
trailing stop reserving the shares, and both PEAK's exit (`PK-`) and the manual flatten (`FL-`)
rejected with *requested 29 available 0*. The manager is fighting itself.

The nine: #239, #245, #252, #254, #265, #285, #287, #295, #303, #313. Same defect, different hat.

---

## The abstraction

One `ProtectionSlot` per `(instrument, strategy)` position. It owns **the** protective order list.
Managers never touch orders — they declare intent:

```python
slot.request(ProtectionSpec(kind=TRAILING, trigger=..., qty=29), by="PEAK")
slot.release(by="FLATTEN")          # flatten is a transition, not a special case
```

`ProtectionSpec` is a value object: kind, trigger, qty, TIF. The slot is the **only** code permitted
to call submit/cancel/modify on a protective order. Enforce it with an AST test over the backend —
the same shape as the `session_runner` guard in `test_qc345_wiring.py`, which proves no construction
anywhere omits an argument. A new manager must be physically unable to bypass this.

### One transition path. No kind dispatch.

```
cancel(incumbent order list)
  → wait until qty_available == qty        # the RESERVATION, not the order status
  → place(successor)
```

An earlier draft of this had three paths, with `PATCH /v2/orders/{id}` for same-kind changes. **That
was wrong and the operator rejected it correctly:** stop kinds change (trailing → stop → stop_limit) and a
bracket is a parent plus two OCO legs, so the modify path would rarely fire and would rot in a corner
nobody hits. One path means every transition exercises the same code.

**Wait on the reservation, not the order status.** `status: canceled` and the shares becoming
available are not the same event, and the next submit needs the shares. Gating on order status is
what produces *requested 29 available 0*. This is #245 in one line.

**Granularity is the ORDER LIST, not the order.** Cancelling one OCO leg either orphans its sibling
or cancels it implicitly, and which is adapter behaviour. Nautilus models this natively
(`OrderList`, `OrderFactory.bracket()`), so we are not inventing it.

---

## The window is accepted, so it must be engineered

Operator, explicitly: *"you complain a lot that it is unprotected for a short time — fine."* Agreed. A
second or two of exposure against a class that has cost nine tickets is the right trade. But
accepting it means three things stop being optional.

### 1. It is a STATE, not a gap

```
PROTECTED  →  SWITCHING  →  PROTECTED
                  ↓  (deadline exceeded, or place failed)
               NAKED + alarm
```

Today the book renders protected/naked purely from whether a resting order exists, so **during a
legitimate transition it says NAKED** — an operator cannot tell a handover from a stripped position.
That is what made 2026-08-12 unreadable. `SWITCHING` carries the lease holder and its age.

### 2. The lease is DURABLE, written before the cancel

If the process dies between cancel and place, the position is naked and nothing knows it was
mid-transition. So: write the lease **before** cancelling, clear it **after** the place confirms. On
startup, any open lease means "we were switching and may have died" → re-protect immediately, log
loudly.

This is the one non-negotiable piece. It is precisely the 2026-08-12 failure — five live positions
left without protection — and the only reason the brief window is dangerous rather than untidy.

### 3. Deterministic client order ids

Keyed on `(position, generation)`, so a replay after a crash is refused by the venue as a duplicate
rather than double-placing. #295 was this missing.

---

## The detector

One invariant, checked every reconcile tick:

> Every held position has **exactly one** resting protective order list, and
> `qty_available + reserved == qty`, unless an open lease explains it.

Violation → alarm and re-protect. This single check would have caught #239, #252, #254, #285, #287
and #303. It is the same shape as the leftover-lots reconciliation that found the `sell_short` bug in
`realized_broker`: **assert the residue, not the process.** Two derivations of one fact — ours and
the broker's — and the disagreement is the finding.

---

## Migration order

Convert callers one at a time, each with the invariant already running so a regression is visible:

1. **flatten** — smallest, and it unblocks the operator's live NBIS problem
2. **#239 backstop** — highest frequency, most incidents
3. **PEAK** — trims and full exit; retires #245 and #254
4. **bracket attach/detach** — order-list granularity gets exercised here
5. **PYRAMID**, **MOMENTUM exits**

Only after all six: add the AST test forbidding direct submit/cancel of protective orders. Adding it
earlier just blocks the migration.

---

## Measure before building — two questions, not assumptions

Both are venue behaviour, which CLAUDE.md says to probe rather than infer (`scripts/probe_trailing_replace.py`
is the existing example):

1. **How long does the reservation take to free after a confirmed cancel?** This sets the deadline and
   the poll interval. If it is not prompt, the window is longer than "a second or two" and that
   changes the risk the operator accepted.
2. **What does cancelling one bracket leg do to its sibling on Alpaca?** Decides whether order-list
   cancel is one call or a loop with its own barrier.

---

## What this does NOT do

It does not decide *what* protection a position should have — that stays with PEAK, the backstop and
the strategies. The slot decides only *how* a change is applied, and guarantees that exactly one
actor is applying one at a time.
