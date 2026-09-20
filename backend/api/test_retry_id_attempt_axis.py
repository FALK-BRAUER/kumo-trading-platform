"""An id that must be stable across a REPLAY must still vary across a RETRY.

WHY THIS FILE EXISTS
--------------------
Nautilus denies a resubmission carrying the same `client_order_id` **locally, before the venue** —
`trading/strategy.pyx:865-868`. The denial journals identically to a genuine venue rejection, so a retry
loop records itself as "tried, refused" while never reaching the broker at all.

Two requirements pull on the same identifier and were collapsed into one deterministic hash, in this repo
and in kumo-trading-strategies, independently:

  * **deterministic for the same logical attempt** — a crash-and-replay must not double-submit;
  * **varying across genuinely different attempts** — a retry after a rejection must not be denied as its
    own duplicate.

`_protection_coid` already carries the `attempt` axis; that is #295's fix, and #295 is why it matters:
a retry on the same coid was denied, the order carried two terminal events, and on the next boot
`load_orders` replayed them and killed `TradingNode` construction on **every** subsequent start until
Redis was cleared by hand.

This file guards that fix from being undone. It does NOT assert the same of `FL-`/`PK-` ids: those mint
one id per command or per manager and have no retry today, so demanding an attempt axis there would be
asserting a requirement that does not yet exist. They are recorded as enhancement G2 instead.
"""

from __future__ import annotations

import inspect


def test_the_protection_id_still_varies_by_attempt():
    """#295's fix, pinned. Removing the parameter makes every protection retry a local duplicate."""
    from api.engine_node import _protection_coid

    params = inspect.signature(_protection_coid).parameters
    assert "attempt" in params, (
        "`_protection_coid` lost its `attempt` axis — a retry then resubmits an id Nautilus already "
        "knows, is denied locally before the venue, and journals as if the venue refused it (#295)"
    )


def test_different_attempts_produce_different_ids():
    """The behaviour, not the signature. A parameter that is accepted and ignored is the same defect."""
    from api.engine_node import _protection_coid

    first = _protection_coid("AAPL.XNAS", "SELL", 0)
    second = _protection_coid("AAPL.XNAS", "SELL", 1)
    assert first != second, (
        "two attempts produced the SAME id, so the second is denied as a duplicate before it reaches "
        "the venue — the attempt parameter is accepted and discarded"
    )


def test_attempt_zero_is_unchanged_so_the_axis_is_additive():
    """`attempt=0` must stay byte-identical, or adding the axis is a migration rather than a fix.

    Same property kumo-trading-strategies adopted for `OrderRequest`: every existing single-attempt id is
    untouched, so nothing already resting at the venue is orphaned by the change.
    """
    from api.engine_node import _protection_coid

    assert _protection_coid("AAPL.XNAS", "SELL") == _protection_coid("AAPL.XNAS", "SELL", 0)


def test_ids_stay_within_nautilus_36_char_limit():
    """The constraint the attempt axis has to fit inside. A truncation collision means one position's
    stop silently replaces another's, and the loser goes naked while the audit shows an order resting."""
    from api.engine_node import _protection_coid

    for attempt in (0, 1, 9, 99):
        coid = _protection_coid("VERYLONGINSTRUMENTIDENTIFIER.XNAS", "SELL", attempt)
        assert len(coid) <= 36, f"attempt={attempt} produced a {len(coid)}-char id: {coid}"
