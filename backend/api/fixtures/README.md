# api/fixtures/

Production-captured inputs for tests that must be driven by what the stack ACTUALLY emits, not by a
hand-built double (CLAUDE.md: a double that cannot represent production is the bug).

- `repair_807/` — the paper stack's Redis cache slice (orders, positions, instruments, index sets;
  raw msgpack, base64) and Alpaca's answer for the same orders/positions, captured 2026-09-09 05:48 UTC.
  Drives `api/test_cache_repair.py`. Read-only: never edit a fixture to make a test pass.

What does NOT go here: synthetic data, secrets, anything larger than a few MB.
- `lanes_bleeding_1098_ibkr_paper.json` — ibkr-paper's `exec_action_log` rows for MOMENTUM-002 and
  BCTROT-004, 2026-09-15..17 (kinds decision/order/risk/state, non-poll slots), captured 2026-09-17
  16:14 UTC: two NO DECISION sessions and ten stop-out rows for three fills. Drives
  `api/test_lanes_bleeding.py`. Read-only.
