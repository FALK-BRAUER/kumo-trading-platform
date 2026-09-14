# repair_807/

Captured 2026-09-09 06:50 UTC from `kumo-paper` (build `5f6b097`, Nautilus 1.229.0) by
`scratchpad/dump_fixture.py` — see #807 for the incident.

- `redis.json` — `trader-COCKPIT-001:{orders,positions,snapshots:positions,instruments,index:*}` for
  the fifteen instruments the sixteen corpse `PROT-SELL-*` orders touched. Values are the
  raw msgpack bytes, base64-encoded; decode with `api.cache_repair.SERIALIZER`, nothing else.
- `venue.json` — Alpaca `GET /v2/orders:by_client_order_id` for every REJECTED-for-a-corpse-reason order (68), `GET /v2/positions`,
  and the open orders, same minute.

Do not regenerate to make a test pass; a new capture is a new fixture with its own date.
