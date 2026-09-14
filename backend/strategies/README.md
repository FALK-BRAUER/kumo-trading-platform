# strategies/

Nautilus `Strategy` subclasses — one per lane. Each is tagged with a unique `StrategyId` for per-lane P&L
attribution, sizes against its capital sleeve, and runs concurrently in the shared `TradingNode`.

Lanes: `MANUAL` (discretionary clicks), `ETF_AUTO` (ETF universe), `BCT_AUTO` (8-condition Blue Flag equities).
Holds: lane strategy classes, shared base class, lane config. Does NOT hold: broker transport (→ `adapters/`),
generic rules (→ `actions/`). All automation gates default `False`.

`momentum.py` wires MOMENTUM-001 from the `kumo-strategies` package into the node. The strategy class
itself lives there, not here — this file is only the assembly: pool and journal from Postgres, orders
through Nautilus, the session fired by the node's own clock. Gate: `KUMO_MOMENTUM_ENABLED`, off by
default -- AND THAT GATE IS THE WHOLE GATE. Enabling it lets it trade: an absent lifecycle row
reads as TRADING (Operator, 2026-08-19), so there is no second operator approval between the deploy and
the first order. This paragraph used to promise one.

`qc345.py` wires QC345-003 the same way, but the assembly is load-bearing rather than convenient
(#324). `QC345RotationStrategy` submits orders itself when `session_runner` is not passed, so
`QC345SessionGateway` — lifecycle, journal/idempotency, risk, budget — is the only thing standing
between registration and unsupervised live orders. Gate: `strategies.QC345_ENABLED` in settings, off by default, and it
refuses to build without a `strategies.QC345_UNIVERSE` in settings rather than registering a strategy
that can never decide. `test_qc345_wiring.py` asserts across the whole backend that no construction
of that adapter omits the runner. It fetches Alpaca's bulk asset list once at build so the ETF/fund
name filter can run — measured 2026-08-17, that removes 3,437 funds the ARCA venue rule alone misses,
including leveraged single-stock ETFs.

`qc345_universe.py` derives QC345's live ranking universe from Alpaca: bulk `/v2/assets`, the
strategy's own fund filter for the substrate (~5.5k symbols), batched daily bars, then
`QC345ComputedSource` does every selection stage. The preservation floors from kumo-strategies#43
are used to CHECK a fetch, never to size one — a cheaper prefiltered fetch would silently drop a
researched name. `scripts/derive_qc345_universe.py` runs it and reports the numbers;
`QC345_UNIVERSE` stays operator-supplied until that measurement is verified.

`qc345_refresh.py` writes `strategies.QC345_UNIVERSE` on a daily Nautilus timer, armed from the feed
actor (the strategy has no event loop at build time) and run in a worker thread — the derivation
measured 373.8s and awaiting it on the loop would stall the node. Gate
`strategies.QC345_UNIVERSE_REFRESH` in settings, off by default — a gate an operator flips in the
UI, not a redeploy. The write is a read-merge-write: `settings.save`
replaces the whole domain, and `strategies` also holds every budget target.
