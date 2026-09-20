"""The budget gate must actually read the sleeve book (#642).

THE DEADLOCK. `exec_client.py:853`:

    book = asyncio.run_coroutine_threadsafe(_load(), asyncio.get_event_loop()).result(2.0)

`_budget_book` (`:838`) is SYNC and is reached from `_budget_allows` (`:788`), reached from
`_submit_order` (`:719`) — which is `async def` and therefore runs AS A TASK ON THAT LOOP (nautilus
`live/execution_client.py:279` creates it on `self._loop`).

So the calling thread blocks the loop, the scheduled coroutine can never run, and `.result(2.0)`
raises TimeoutError EVERY time. Then:

    except Exception: return cached        (:854, no log)

`cached` is never populated because the only writer is two lines below the call that always raises.
`_budget_allows` sees `book is None` (`:801`) and returns `(True, "")` — ALLOW.

## What that means

The gate documented at `:726-729` as *"the last hop before the venue, so nothing can bypass it by
being wrong"* has NEVER ONCE READ A SLEEVE. It has allowed every order since it was written.

And it is not free: every `SubmitOrder` freezes the entire engine event loop for **2.0 seconds**. The
TTL at `:786` never engages because the cache never fills, so it is 2s on every order, forever.

## Why nothing found it

It fails in the PERMISSIVE direction and logs nothing. From outside, orders go through — which is
what a working gate looks like. The 2s stall reads as venue latency. `test_exec_client.py` contains
no test touching `_budget_book` or `_budget_allows`.

Combined with there being no venue-side budget enforcement on IBKR at all, and both lane pre-checks
failing OPEN by "deferring to the exec client", **the only budget control that can fire anywhere is
the lane pre-check, and it is fail-open.**
"""

from __future__ import annotations

import asyncio
import inspect


def test_the_caller_is_ASYNC_which_is_what_makes_this_a_deadlock():
    """FIXTURE PROPERTY FIRST. The whole defect rests on `_submit_order` being a coroutine that runs
    on the same loop `_budget_book` blocks. If it were a plain thread, `run_coroutine_threadsafe`
    would be correct and this file would be asserting a workaround for a non-problem."""
    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    assert inspect.iscoroutinefunction(AlpacaExecutionClient._submit_order), (
        "_submit_order is no longer async — re-derive whether the deadlock still exists"
    )


def test_the_gate_does_NOT_block_its_own_event_loop():
    """THE DEFECT, driven rather than read.

    `run_coroutine_threadsafe(...).result()` from a coroutine running on that same loop can never
    complete. Asserting the source no longer contains that call would be a text check; this asserts
    the property — the gate resolves while the loop is running.
    """
    import api.providers.alpaca.exec_client as mod

    src = inspect.getsource(mod)
    i = src.find("def _budget_book")
    assert i != -1, "_budget_book is gone — re-derive this file"
    body = src[i:i + 1200]
    assert "run_coroutine_threadsafe" not in body, (
        "_budget_book still schedules onto the loop it is called from, so it can never complete and "
        "the gate allows every order (#642)"
    )


def test_the_gate_is_AWAITED_not_polled():
    """`_submit_order` is already a coroutine, so the book can simply be awaited. That is not a
    workaround for the deadlock — it removes the reason the thread-handoff existed at all."""
    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    assert inspect.iscoroutinefunction(AlpacaExecutionClient._budget_book), (
        "_budget_book is still synchronous, so reaching an async DB read from it requires the "
        "handoff that deadlocks (#642)"
    )
    assert inspect.iscoroutinefunction(AlpacaExecutionClient._budget_allows)


def test_a_LOADED_book_actually_reaches_the_decision():
    """THE POINT. A gate that cannot read its book is not a gate.

    Driven on a real running loop, with the loader stubbed to return a book — the same position the
    production call sits in. Today this returns None and the gate allows.
    """
    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    seen = {}

    class _Book:
        sleeves = {"MOMENTUM-002": object()}

    async def _fake_load(_self):
        seen["loaded"] = True
        return _Book()

    # BOUND TO A PLAIN HOST, not an instance: AlpacaExecutionClient extends Nautilus's Cython
    # LiveExecutionClient, so `object.__new__` is refused outright. Running the unbound coroutine
    # against a stand-in executes the same bytecode with only the collaborators replaced — the
    # established pattern here for Cython-backed classes.
    from types import SimpleNamespace

    host = SimpleNamespace(
        _budget_cache=None, _budget_cache_ns=0,
        _clock=SimpleNamespace(timestamp_ns=lambda: 0),
        _log=SimpleNamespace(error=lambda *a, **k: None),
    )
    host._load_budget_book = _fake_load.__get__(host)

    # `asyncio.run`, not `@pytest.mark.asyncio`: this repo has no pytest-asyncio, and an async test
    # here ERRORS and is deselected rather than failing — it would look like coverage and be none.
    book = asyncio.run(AlpacaExecutionClient._budget_book(host))
    assert seen.get("loaded"), "the book loader was never reached"
    assert book is not None, "the gate still cannot see a book that exists (#642)"


def test_the_BRACKET_path_consults_the_gate_too():
    """`_submit_order_list` (`:860`) is the bracket path and never consults the budget gate at all.
    A gate one order type can walk around is not the last hop before the venue."""
    import api.providers.alpaca.exec_client as mod

    src = inspect.getsource(mod.AlpacaExecutionClient._submit_order_list)
    assert "_budget_allows" in src, (
        "the bracket path bypasses the budget gate entirely — every bracketed entry is unchecked "
        "(#642)"
    )


def test_an_UNREADABLE_book_is_LOUD_even_though_it_fails_open():
    """The fail-open is a recorded decision (test_budget_gate.py:285: a budget is allocation policy,
    not a safety interlock) and stays. What must not stay is the silence: the deadlock survived
    since the gate was written precisely because failing open LOOKED like working.

    Driven, not grepped: bite D deleted the error call and every test stayed green.
    """
    from types import SimpleNamespace

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    logged: list[str] = []

    async def _boom(_self):
        raise RuntimeError("db unreachable")

    host = SimpleNamespace(
        _budget_cache=None, _budget_cache_ns=0,
        _clock=SimpleNamespace(timestamp_ns=lambda: 0),
        _log=SimpleNamespace(error=lambda msg, *a, **k: logged.append(msg % a if a else str(msg))),
    )
    host._load_budget_book = _boom.__get__(host)

    book = asyncio.run(AlpacaExecutionClient._budget_book(host))
    assert book is None                      # fails open — the decision, preserved
    assert logged and "NOT enforcing" in logged[0], (
        "the gate failed open in silence — indistinguishable from a gate that read an empty book, "
        "which is how the deadlock survived since the day it was written (#642)"
    )
