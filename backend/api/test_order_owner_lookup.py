"""Whose order is this? — answered from the CACHE, never from the client-order-id prefix (#748).

`coverage_by_lane` and `oversize_by_lane` take an `owner_of(client_order_id)` callback. This is that
producer, and the review that specified it was explicit about the one way to get it wrong.

DO NOT REUSE `_owner_of` / `owner_from_prefix`. Those exist for CANCEL AUTHORISATION, where the
question is "may this caller cancel it" and answering "a PROT- order is account protection, MANUAL-001
may touch it" is the safe direction. For COVERAGE ATTRIBUTION the same fallback is poison, because the
question is "whose shares are reserved" and a prefix cannot know.

The failure is silent and bidirectional. Take a cache-terminal per-lane stop — genuinely MOMENTUM's,
invisible to the cache. A prefix-style lookup names it MANUAL-001, so MOMENTUM reads NAKED (the next
pass rests a duplicate stop over reserved shares, which oversells on trigger) and MANUAL reads
OVER-COVERED (lane-aware oversize shrinks a stop that is not there to shrink) — with `is_complete`
True throughout, so nothing reports a problem.

Returning None is what makes the refuse-to-split machinery work at all.
"""

from __future__ import annotations

from types import SimpleNamespace


class _Cache:
    """A cache that REFUSES what production refuses.

    Nautilus's `Cache.order` is Cython-typed and rejects a raw `str` with a TypeError. The first
    version of this double accepted strings, so it could not represent that refusal — and the
    producer, which passed a raw str, returned None for every order in production while all twelve
    tests here passed. Fix the double, never loosen production to suit it.
    """

    def __init__(self, by_coid=None, by_venue=None):
        self._c = by_coid or {}
        self._v = by_venue or {}

    def order(self, coid):
        from nautilus_trader.model.identifiers import ClientOrderId

        if not isinstance(coid, ClientOrderId):
            raise TypeError(
                f"Argument 'client_order_id' has incorrect type (expected ClientOrderId, "
                f"got {type(coid).__name__})"
            )
        return self._c.get(str(coid))

    def venue_order_id(self, coid):        # not used by the lookup; present so the double cannot
        return None                        # accidentally satisfy a shape production does not have

    def orders(self):
        return list(self._c.values())


def _order(sid):
    return SimpleNamespace(strategy_id=sid)


def _lookup(cache):
    from api.engine_node import UiFeedStrategy

    fake = SimpleNamespace(cache=cache)
    return UiFeedStrategy._order_owner_of.__get__(fake, type(fake))


def test_an_order_the_cache_KNOWS_is_named_by_its_own_strategy_id():
    owner = _lookup(_Cache({"PROT-SELL-AEM-1": _order("MOMENTUM-002")}))
    assert owner("PROT-SELL-AEM-1") == "MOMENTUM-002"


def test_an_order_the_cache_DOES_NOT_KNOW_is_None_not_guessed_from_the_prefix():
    """The whole point. A `PROT-` id tells you cockpit placed it; it does not tell you whose shares it
    reserves. Naming it MANUAL-001 here is confidently wrong in both directions at once."""
    owner = _lookup(_Cache({}))
    assert owner("PROT-SELL-AEM-1") is None


def test_a_BRACKET_LEG_carrying_a_VENUE_MINTED_id_is_None_rather_than_mis_named():
    """We submit a bracket as one native request and the VENUE mints the child ids, so the cache does
    not hold them under our coid. Unknown, not guessed."""
    owner = _lookup(_Cache({"PROT-SELL-AEM-1": _order("MOMENTUM-002")}))
    assert owner("379fcfb5-6e6d-4947-8666-55e061c20b02") is None


def test_an_EMPTY_client_order_id_is_None():
    owner = _lookup(_Cache({"": _order("MOMENTUM-002")}))
    assert owner("") is None


def test_an_order_with_NO_strategy_id_is_None_not_the_empty_string():
    """An empty owner would land in `by_lane[""]` and be treated as a lane — the blank-lane shape this
    whole area exists to eliminate."""
    owner = _lookup(_Cache({"X": SimpleNamespace(strategy_id=None)}))
    assert owner("X") is None
    owner2 = _lookup(_Cache({"X": SimpleNamespace(strategy_id="")}))
    assert owner2("X") is None


def test_a_CACHE_THAT_RAISES_is_None_rather_than_taking_down_the_protection_tick():
    """This runs inside the 60s protection reconciler. A lookup that raises would take the whole tick
    with it — a protection outage caused by an attribution question."""
    class _Angry:
        def order(self, coid):
            raise RuntimeError("cache unavailable")

    assert _lookup(_Angry())("PROT-SELL-AEM-1") is None


def test_THE_LOOKUP_NEVER_CONSULTS_A_PREFIX():
    """Pinned as a property, not a behaviour: the source must not reference the prefix helpers at all.
    A future edit reaching for `owner_from_prefix` to 'improve coverage' would reintroduce exactly the
    silent bidirectional error this producer exists to avoid."""
    import ast
    import inspect
    import textwrap

    from api.engine_node import UiFeedStrategy

    # BOUND TO THE AST, NOT TO SOURCE TEXT. The first version substring-matched the whole function and
    # failed on its own DOCSTRING, which names these helpers precisely to say do not use them — a
    # check satisfied by deleting an explanation and broken by writing one. Same correction this
    # session already made twice elsewhere.
    tree = ast.parse(textwrap.dedent(inspect.getsource(UiFeedStrategy._order_owner_of)))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("owner_from_prefix", "OUR_STOP_PREFIXES", "startswith"):
        assert banned not in used, (
            f"the coverage owner lookup CALLS {banned!r} — that is cancel-authorisation vocabulary, "
            f"and using it here names an unknown order MANUAL-001"
        )
