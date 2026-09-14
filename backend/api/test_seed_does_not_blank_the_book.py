"""A restart blanked the book for five minutes, and the seed took five minutes to fail (#74b.2).

MEASURED 2026-08-26, both tenants:

    paper    engine started 11:19:37   "trade-cycle restart seed failed (starting fresh): TimeoutError()" 11:24:07
    staging  engine started 11:22:03   same, 11:27:28

4m30s and 5m25s. For that entire window `_publish_trades` returns:

    {"trades": [], "status": "seeding"}

The UI does `data.trades.filter(t => t.is_engaged)`, so the Portfolio tab showed ONE row — the
unclaimed broker position — while 32 strategy-owned positions were held. The operator found it by opening the
app; `/health` said `ok` throughout.

TWO DEFECTS, and I misdiagnosed both before measuring:

1. THE SEED IS UNBOUNDED. It is a best-effort restore of cycle BOUNDARIES from the durable envelope.
   Nothing about it should be able to hold the book hostage for five minutes, and nothing bounded it.

2. SEEDING PUBLISHES AN EMPTY BOOK RATHER THAN THE LAST KNOWN ONE. `self._last_good_trades` already
   exists and is maintained on every successful projection — and the seeding branch returns before
   reaching it. An empty array is not "we do not know yet"; it renders as "you hold nothing".

I first claimed this was IBKR-specific, reasoning that `exec_client_id is None` left `_trade_cycles`
permanently empty. It was not: staging wrote a trade_cycle at 11:28:24 on that very boot, so the dict
was populated. The symptom was identical on both tenants because the cause was common to both.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).parent / "engine_node.py"


def _method(name: str) -> str:
    src = SRC.read_text()
    i = src.index(f"async def {name}(self)") if f"async def {name}(self)" in src else src.index(f"def {name}(self)")
    ends = [x for x in (src.find("\n    def ", i + 10), src.find("\n    async def ", i + 10)) if x > 0]
    return src[i:min(ends)]


def test_the_fixture_can_see_both_methods():
    src = SRC.read_text()
    assert "async def _load_seed_bounded" in src and "_last_good_trades" in src, (
        "the seed or the last-good buffer moved — this file is blind")


def test_the_SEED_IS_TIME_BOUNDED():
    """It took 4m30s and 5m25s to fail on the two tenants. A best-effort restore of cycle boundaries
    must not be able to hold the book for minutes.

    Note what the bound could NOT fix: on 2026-08-26 it expired every boot while the seed's actual work
    was 60ms, because the coroutine was queued on a loop saturated by backfill. A bound on a thing that
    never starts measures the queue. That is #568; this test only pins that the bound exists."""
    # NOT just the word "timeout" — the body already contains `socket_timeout` for the redis client,
    # and that passed vacuously while the seed ran unbounded for five minutes. The per-socket timeout
    # is not a bound on the WHOLE seed: it retries, reconnects, and reads from Postgres too.
    # THE CALL, NOT THE NAME. `_SEED_TIMEOUT_SECS` also appears in the except-branch's log message, so
    # removing the `wait_for` left this green — the sixth time today that a mention of a thing
    # satisfied a check for the thing. Walk the AST for an actual `asyncio.wait_for(...)`.
    import ast

    # THE BOUND MOVED, THE INVARIANT DID NOT (#568). It used to wrap `_seed_cycles` on the node loop,
    # where `asyncio.wait_for` measured the SCHEDULER QUEUE rather than the work — the seed waited
    # 4m47s for a slot and the 20s budget expired without the seed ever running. It now wraps
    # `_load_seed` on the seed thread, so the budget bounds the WORK, which is what it was always for.
    body = _method("_load_seed_bounded")
    tree = ast.parse("class X:\n" + "\n".join("    " + ln for ln in body.splitlines()))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "wait_for"]
    assert calls, (
        "the seed is not wrapped in asyncio.wait_for, so nothing bounds it AS A WHOLE — a slow "
        "restore blanks the book for as long as it takes")
    assert any(getattr(a, "attr", None) == "_SEED_TIMEOUT_SECS" for c in calls for a in c.args), (
        "wait_for is called with something other than the declared bound")


def test_the_BOUND_IS_SECONDS_NOT_MINUTES():
    """A bound larger than the observed failure time would be no bound at all."""
    src = SRC.read_text()
    tree = ast.parse(src)
    val = next((n.value.value for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "_SEED_TIMEOUT_SECS" for t in n.targets)
                and isinstance(n.value, ast.Constant)), None)
    assert val is not None, "_SEED_TIMEOUT_SECS is not a plain constant"
    assert 0 < val <= 30, (
        f"{val}s does not bound anything useful — the observed failures were 270s and 325s")


def test_SEEDING_serves_the_LAST_KNOWN_book_not_an_empty_one():
    """`_last_good_trades` is already maintained on every successful projection. The seeding branch
    returned before reaching it, so a restart rendered as 'you hold nothing' rather than 'this is what
    you held a moment ago'."""
    # THE VALUE PUBLISHED, not a nearby mention. The comment explaining the fix names
    # `_last_good_trades`, so reverting the value left this green.
    import ast

    body = _method("_publish_trades")
    tree = ast.parse("class X:\n" + "\n".join("    " + ln for ln in body.splitlines()))
    seeding = [d for d in ast.walk(tree) if isinstance(d, ast.Dict)
               and any(isinstance(k, ast.Constant) and k.value == "status" for k in d.keys)
               and any(isinstance(v, ast.Constant) and v.value == "seeding" for v in d.values)]
    assert seeding, "no seeding payload found — this test is blind"
    trades_value = next(v for d in seeding for k, v in zip(d.keys, d.values)
                        if isinstance(k, ast.Constant) and k.value == "trades")
    assert not (isinstance(trades_value, ast.List) and not trades_value.elts), (
        "the seeding branch publishes a literal empty list — an empty book is a CLAIM, and during a "
        "restart it is a false one")


def test_the_STATUS_still_says_seeding_so_the_tile_can_label_it():
    """Serving the last known book must not pretend it is current. The rows come back; the status
    still says they are not fresh."""
    body = _method("_publish_trades")
    assert '"seeding"' in body, (
        "the seeding status is gone — the tile would render stale rows as live ones")
