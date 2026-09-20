# Design — the market-data contract

2026-08-28: *"you need a nice architecture to not make this endless if/then/else all over the
code"* — and *"flexible, encapsulated, minimal code changes across the app"*.

Baseline `v0.0.0` = `1a94d97`. Plan and step order: `PLAN_618_DATA_NEEDS.md`. This file is the shape.

## The problem this solves

Every consumer today asks for a BAR TYPE, which means every consumer has already assumed a mechanism
that only one venue has. That assumption is why five granularities are structurally dead on IBKR, why
`feed=iex` rations an IBKR node (#619), and why the UI cannot tell "no data yet" from "never here".

## One idea

**A consumer states how stale it can tolerate. The adapter states what it can guarantee. Nobody above
the adapter names a venue or a mechanism.**

## The four types

```
Need     { max_staleness, criticality, on_miss }     declared by the consumer, next to the code that uses it
Offer    { mechanism, guaranteed_ceiling }           declared by the adapter, per provider
Binding  { need, offer, cadence, measured }          what the resolver actually bound
Reading  { value, observed_at, ceiling, measured }   what every consumer receives, ALWAYS this shape
```

`on_miss` is DERIVED from criticality, never configured: anything that places an order refuses,
anything that draws degrades. A knob whose off-position is a known defect is not a knob.

## Three rules that remove the branches

### 1. Exactly one `if`, and it lives in the adapter

Mechanism selection is a real conditional and belongs one place per provider, below the connector:

```python
# providers/ibkr.py — the ONLY if/else about IBKR anywhere
ceiling <= 1s    -> tick_by_tick        (entitlement/cap may refuse -> fall to the next row)
ceiling <= 1min  -> 1m keepUpToDate
else             -> historical
```

Above it nobody asks which venue. `DataClientSpec` (`providers/base.py`) is already the
provider-owned contract and already reaches `build_node` — it simply carries no capability yet.
**#619 is the first slice of exactly this**: the realtime subscription budget stops being an Alpaca
constant reachable from every provider and becomes a number the provider declares.

### 2. A uniform envelope, so callers have nothing to branch on

The sprawl comes from `value | null`: every consumer then writes `if (price)`. Kill the null.
Consumers never unwrap — they hand a `Reading` to one component that renders the value AND its
staleness affordance. One implementation, every tile. The ~20 hand-written "is this stale / missing"
checks collapse into it.

### 3. `refuse` is an absence, not a state

When criticality says refuse, the resolver DOES NOT DELIVER. The strategy has no bar and its existing
no-data path runs — already written, already tested. So `degrade` is carried in the envelope,
`refuse` is carried by the data not being there, and **neither adds a conditional above the adapter.**

## Cadence is a fact; `stale` is the only fault

"Degraded" conflated *slower than ideal* with *slower than required*. Only the second is a fault.

```
● live · tick      sub-second
● live · 1m        IBKR today — correct, complete, just paced
● live · 1d
● stale            measured > the consumer's declared ceiling — THE ONLY ALARM
○ no feed          INERT (#613)
```

`1m` needs no adjective. Calling it degraded would import Alpaca's cadence as the definition of
healthy — the same venue-centric thinking as `feed=iex` on an IBKR node. The state space collapses to
*cadence is data, `stale` is arithmetic*.

## The claim is continuously falsifiable

Staleness is observable: we already keep `_last_tick_ts` per symbol. Measured inter-arrival vs
declared ceiling is two derivations of one fact — the detector, not the hazard.

- venue declares `<=1s`, delivers 40s -> the declaration is a lie, **detectable with no error code**
- downgrade automatically, announce it, name the number that caught it

This matters because tonight's IBKR bar path produced NO error at all. It quietly served nothing. A
declared-vs-measured check catches that on the first poll instead of after a night of hunting.

## What enforces it

Two class-aimed tripwires, in the style already used here:

- **no venue name above the connector** — no module outside `providers/` mentions ibkr/alpaca/
  databento in CODE (comments stripped via the AST trick in `test_feed_freshness_is_venue_neutral`).
  Turns #616 from a ticket into a tripwire. Four known instances must be fixed first: `:977`, `:1160`,
  `:6682`, and `:1187` (#619).
- **no consumer unwraps a Reading** — a `.value` read outside the render component is the branch
  growing back.

## Honest costs

- Codex disproved "zero UI changes": tiles bind bars through `useInstrument()`/`useBars()`
  (`instrument.ts:202/207`), so the envelope touches every consumer once — **wide but shallow, and
  mechanical**. The tripwire is what stops it regrowing.
- Backend consumers read bars with NO WS topic at all (`_last_price_and_ts_for:4237`, PEAK/PYRAMID
  `:7454/:7607/:7726`). They must be explicit non-display OWNERS of their needs, or a display
  refcount can starve a trading input. This is the single most dangerous part of the whole design.
- `_bar_types` is append-only and `_on_refetch` heals forever (`:669/:1211/:1245`). Demand-driven
  removal is impossible until that is addressed.
- WS close does not release topics (`app.py:1295`); only explicit unsubscribe does (`:1258`). A
  refcount would leak on every dropped socket.

## Order

`#619` (capability on the spec, one number) -> price plane accepts a bar as evidence -> refuse the
granularity default in BOTH places (`app.py:136`, `datasources.ts:73`) -> declared needs replace the
chart config -> the Reading envelope + banner -> demand-driven binding behind a default-False flag.

Each is independently shippable and independently revertable. The first three change no architecture
and are worth shipping on their own.
