# repair_909/

Captured 2026-09-10 20:40 UTC from `kumo-staging2` (cockpit `4a4f6f7`, Nautilus 1.229.0) by
`scratchpad/dump_gld_fixture.py` — see #909. The GLD.ARCX slice only.

- `redis.json` — `trader-COCKPIT-STG:{orders,positions,snapshots:positions,instruments,index:*}`
  for GLD.ARCX: the synthetic `GLD.ARCX-EXTERNAL` leg Nautilus minted at the 20:16:37Z boot (one
  `reconciliation=True` fill, `S-` ids, 23 @ 426.23), BCTROT-004's 23 (09-09 transfer), TECHIVOL-005's
  39 (real fill), and the seven GLD orders. Raw msgpack bytes, base64-encoded.
- `venue.json` — IB `reqPositions` the same minute, in the shape `VenueTruth.from_fixture` reads:
  GLD 62 long. IB reported exactly one GLD execution that day (the 39).

Do not regenerate to make a test pass; a new capture is a new fixture with its own date.
