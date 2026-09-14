# api/fixtures/

Production-captured inputs for tests that must be driven by what the stack ACTUALLY emits, not by a
hand-built double (CLAUDE.md: a double that cannot represent production is the bug).

- `repair_807/` — the paper stack's Redis cache slice (orders, positions, instruments, index sets;
  raw msgpack, base64) and Alpaca's answer for the same orders/positions, captured 2026-09-09 05:48 UTC.
  Drives `api/test_cache_repair.py`. Read-only: never edit a fixture to make a test pass.

What does NOT go here: synthetic data, secrets, anything larger than a few MB.
