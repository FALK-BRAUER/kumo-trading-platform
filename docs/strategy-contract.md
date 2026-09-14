# The strategy contract

What a strategy must do to be registered, funded and enforced by the cockpit — and, as importantly,
what it must *not* try to do.

Written 2026-08-15 alongside #318 (registration) and #320 (budgets). Companion to ADR `0001`
(execution ownership) and the lifecycle spec, kumo-strategies#187.

---

## The one-sentence version

**The strategy declares; the platform decides.**

A strategy is never asked to be *trustworthy* about a limit. It is asked to be *honest* about what it
is doing. Everything below follows from that.

---

## 1. Registration

Declare the strategy in `api/strategy_registry.py`. That is a code change and always will be — the
strategy class is code. What is **not** a code change is turning it on, funding it, or configuring it.

```python
StrategyEntry(
    name="QC345", tag="003", settings_domain="qc345",
    title="QC345 — monthly top-down momentum rotation.",
)
```

### `order_id_tag` is allocated, not chosen

It must be unique across every strategy in one trader. It is the suffix of the `StrategyId` **and** of
every generated client order id, and Nautilus enforces it at `Trader.add_strategy` — a collision does
not degrade, **the node does not boot**:

```
order_id_tag conflict for '001'
```

`next_free_tag()` allocates. `validate()` refuses duplicates and refuses reassigning a tag that live
positions are keyed to. MANUAL holds `001` and cannot move, because the NETTING position id is
`{instrument}-{strategy_id}` and changing it re-homes every position it owns.

### Conform to the Protocol

`api/strategy_contract.RegistrableStrategy`. Four members, each because the platform needs it and
cannot derive it:

| member | why the platform cannot work it out |
|---|---|
| `strategy_id` | keys positions, cycles and sleeves |
| `claimed_instruments` | `external_order_claims` are node-exclusive; only the platform sees the overlap |
| `warmup_bars` | so "not ready" is shown rather than inferred from a funded strategy holding nothing |
| `is_entry(...)` | a SELL is an exit on a long and an *entry* on a short — only the strategy knows |

`conforms()` names what is missing rather than reporting a bare failure.

---

## 2. Budget

Two numbers per strategy. The gap between them is the instruction.

| | owner | changes when |
|---|---|---|
| `target` | the `strategies` settings domain — **intent** | an operator types it. Instantly. Moves no capital |
| `actual` | `strategy_sleeve` — **reality** | a fill genuinely frees or commits capital |

```
actual > target   reduce — the excess leaves on each sell
actual < target   grow into it as capital arrives
target == 0       wind down: stop opening, keep exiting, hand back what comes free
```

### A strategy must not check its own budget

This is the part that matters. A strategy that polices its own allocation does so correctly until the
day it has a bug — and then it holds more than it was granted, silently, because nothing else was
watching.

Enforcement is `budget_gate.may_submit`, on the path every order takes. Over budget:

- **entries are refused**
- **exits are always allowed**

The asymmetry is deliberate. A strategy over its budget *needs* to sell; a gate that blocked its sells
would trap it above target permanently, which is the opposite of what the operator asked for when they
lowered the number.

### Denial is normal

A strategy **will** be refused entries while it is over target. That is not an error.

A strategy that logs an error, retries, or halts itself on refusal turns an ordinary wind-down into an
incident. Treat a refusal the way you would treat "no candidates passed the filter today": nothing
happened, and nothing is wrong.

### A transition, end to end

1. Operator sets the donor's target down and the recipient's up. Nothing moves.
2. The donor is now over budget → its entries are refused, its exits are not.
3. On each donor sell, the **excess** transfers to the recipient — capped by what was actually freed
   and by the recipient's headroom.
4. The recipient's `actual` grows and it can deploy that much, and no more.

Total allocated capital is invariant throughout, which is why the overlap window cannot double the
book's exposure.

---

## 3. What Nautilus already enforces, and what it does not

`TradingState.REDUCING` (`risk/engine.pyx:1150`) denies orders that would increase exposure, inside the
RiskEngine, where no strategy can bypass it. Use it for the **node-wide** case — the #108 kill switch:
stop opening everywhere, keep exiting.

It cannot express a per-strategy budget:

- `trading_state` is a **single field on the RiskEngine** — node-wide. Setting it to wind one strategy
  down freezes entries for all of them.
- It gates on `portfolio.is_net_long(instrument_id)` — per instrument **direction**, not per sleeve. It
  answers "would this increase exposure", not "has this strategy spent its allocation".

The two compose. A strategy inside its own budget still cannot open while the node is REDUCING.

---

## 4. Lifecycle

Whether a strategy may act is **database state**, not an environment variable and not a code change:

```
DISABLED --enable--> WARMUP --history ready--> SHADOW --operator--> TRADING
   ^                                             ^                     |
   |                                             +--pause--------------+
   +-- LIQUIDATING <--flatten-- HALTED <--risk breach / disconnect -----+
```

- `SHADOW` is the dry run: full decision path, publishes what it *would* do, submits nothing. It is how
  a strategy earns trust on live data rather than on a backtest.
- **Only an operator may enter `TRADING`.** Everything can stop a strategy; nothing can start it.
- `LIQUIDATING` is exits-only, and reaches `DISABLED` automatically once flat.

Lifecycle and budget are **orthogonal**: lifecycle says *may it act*, budget says *how much*. A partial
wind-down is a budget change, not a lifecycle state — the strategy stays `TRADING` and keeps rotating
inside a smaller sleeve.

---

## 5. Checklist

- [ ] Declared in `strategy_registry.py` with an allocated tag; `validate()` passes
- [ ] Satisfies `RegistrableStrategy`; `conforms()` returns no missing members
- [ ] `claimed_instruments` does not overlap another strategy's — the node will not boot if it does
- [ ] A `<name>.schema.json` settings domain, so no parameter is hardcoded
- [ ] An entry in the `strategies` budget schema — otherwise it renders no field and can never be funded
- [ ] Treats a budget refusal as normal
- [ ] Reports warmup honestly and refuses to decide before it
- [ ] Position state driven by order **events**, never by the fact that submit was called
