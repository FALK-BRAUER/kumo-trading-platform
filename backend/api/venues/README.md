# api/venues/

What Alpaca and Interactive Brokers ACTUALLY do, as executable facts — and doubles built from them.

## Why this exists

Six test doubles lied in a single session on 2026-08-24, each producing a confident, plausible, wrong
answer:

| the lie | what it hid |
|---|---|
| `Portfolio.equity` returned a float | production returns `dict[Currency, Money]`; `float(dict)` raised into a debug log, so a whole branch was dead on every venue |
| `_Account` had no `balance_total` | "trust total on every venue" mutation left the file green; three tests passed for the wrong reason |
| `balance_free` answered ANY currency | Nautilus raises for a currency the account lacks; hid that `_publish_account` returned three lines before the code under test |
| `strategy_positions()` returned `{}` | runner believed the book was empty and formed 8 fresh BUYS instead of exits |
| `positions()` read the CLAIMS table | claims go stale; two lanes formed 156 BETA against 79 held — a naked short |
| `execute()` returned a `list` | SQLAlchemy returns a `Result` with `.all()`; the never-raises guard swallowed it |

Every one was a double inventing its own idea of the venue. This package removes that freedom: doubles
are BUILT from the catalogue, so a double that accepts what the venue refuses cannot be written by
accident.

## What is NOT here, deliberately

**A matching engine.** Nautilus has one — `BacktestEngine` with `add_venue`, plus `models/fill.pyx`,
`models/fee.pyx` and `models/latency.pyx`. Building another would reimplement the hard, well-tested
part and lose it. For a backtest, configure Nautilus's venue and layer these facts on top.

## Provenance is part of the fact

Every entry records HOW it was established, because a fact with no provenance cannot be rechecked when
it stops being true:

    MEASURED   read off the live venue or a running container — the strongest kind
    SOURCE     read from the installed package, which cannot be wrong about our pinned version
    INCIDENT   inferred from a production failure, with the date and what it cost

`test_facts_still_hold.py` re-derives every SOURCE fact from the installed package on every run, so
the catalogue fails loudly when an upgrade changes the venue underneath us — rather than rotting into
a comment nobody trusts.

## Mutation-testing this package

Bites must clear `__pycache__`, and that is not fussiness. While bitings these files I restored the
source and the suite still failed — pytest was importing the MUTATED bytecode. The failure direction
was harmless that time, but the same mechanism runs the other way: a bite that appears to SURVIVE
because the cached original is still being imported reads as "this guard is unnecessary" and invites
someone to delete it.

    find . -name __pycache__ -path '*venues*' -exec rm -rf {} +

A mutation test whose result depends on a cache is not a measurement.
