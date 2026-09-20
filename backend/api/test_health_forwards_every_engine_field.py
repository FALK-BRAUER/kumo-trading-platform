"""Every engine-owned key the health model declares must actually be FORWARDED (#546 class).

THERE ARE THREE FIELD LISTS ON ONE PAYLOAD, and that is the actual defect. The engine publishes a
dict; `consumer.health()` copies keys out of the bus frame one by one; `/health` names them again
building the response. A key missing from ANY of the three is empty forever while the other two are
correct. `unpriced_positions` and `failed_requests` shipped with tests on the engine, the model and
the endpoint — and were still empty on both live stacks, because nothing covered the middle hop.

`/health` builds its response from an explicit field list, so a key the engine publishes and this
endpoint does not name is empty forever — while the engine is correct, the model is correct, and
every test of both passes. That has now happened FIVE times: #233, #322, #336, #546, and #618, the
last one in a commit whose own comment six lines above the omission described the trap.

SO THIS DOES NOT NAME FIELDS. Naming them is what failed: each fix added one name to a list nobody
could keep complete. This drives the real handler with an observation carrying every engine-owned key
and asserts the value SURVIVES — so a field added tomorrow is covered without editing this file.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

import api.app as app_mod
from api.models import HealthResponse

#: Keys `/health` is expected to source from somewhere OTHER than the engine observation — probed
#: live by the API itself, or derived. Everything else must come through untouched.
NOT_FROM_THE_ENGINE = {
    "status",       # derived from the subsystems and the degraded conditions
    "subsystems",   # probed live by the api process
}


def _engine_owned_fields() -> set[str]:
    return set(HealthResponse.model_fields) - NOT_FROM_THE_ENGINE - _splatted_fields()


def _health_call() -> ast.Call:
    """The single `HealthResponse(...)` construction inside the handler."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(app_mod.health)))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "HealthResponse"
    ]
    assert len(calls) == 1, f"expected one HealthResponse construction, found {len(calls)}"
    return calls[0]


def _splatted_fields() -> set[str]:
    """Keys supplied by a `**helper()` splat rather than named one by one.

    `_provenance()` fills the four image-stamp fields this way, and they are populated on both live
    stacks (read back 2026-08-31: cockpit_sha 5393ce4, digest 17b3c27b1409da1b). Resolving the splat
    rather than exempting the names keeps the check honest: if that helper stops returning one of
    them, the field falls back into the set below and this file fails.
    """
    out: set[str] = set()
    for kw in _health_call().keywords:
        if kw.arg is not None:
            continue
        fn = getattr(kw.value.func, "id", None) if isinstance(kw.value, ast.Call) else None
        helper = getattr(app_mod, fn, None) if fn else None
        if helper is None:
            continue
        htree = ast.parse(textwrap.dedent(inspect.getsource(helper)))
        for node in ast.walk(htree):
            if isinstance(node, ast.Dict):
                out |= {k.value for k in node.keys if isinstance(k, ast.Constant)}
            # `out["cockpit_digest"] = ...` — assigned after the literal, still supplied.
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                out.add(node.slice.value)
    return out


def test_the_fixture_can_express_the_bug():
    """Vacuity guard: there must BE engine-owned fields, or every assertion below is about nothing."""
    assert _engine_owned_fields(), "no engine-owned health fields — this file asserts nothing"


def test_every_engine_owned_field_is_NAMED_in_the_construction():
    """The construction is a literal field list. A model field absent from it can never be populated,
    whatever the engine sends — which is the whole defect, and it is visible statically."""
    named = {kw.arg for kw in _health_call().keywords if kw.arg}
    missing = sorted(_engine_owned_fields() - named)
    assert not missing, (
        f"{missing} are declared on HealthResponse and never passed by /health, so they are empty "
        f"forever no matter what the engine publishes. This is the #546 class; do not fix it by "
        f"adding just these names — every previous fix did that and the next field was dropped too."
    )


@pytest.mark.parametrize("field", sorted(_engine_owned_fields()))
def test_the_field_is_not_a_HARDCODED_CONSTANT(field):
    """Named in the construction, and carrying something.

    A field NAMED and wired to a literal passes the check above while reporting nothing — the
    agreement-is-not-connection shape, and the reason naming alone is not enough.

    IT DOES NOT REQUIRE THE LITERAL WORD `observed`. Three fields legitimately reach it by another
    route: `feed_last_tick_ts` reads the observation under a different key (`last_tick_ts`), `inert`
    comes from a helper that takes it, and `ownership_violations` takes `inert` — which is itself
    derived from the observation. Demanding a textual mention would have forced those three to be
    exempted BY NAME, which is the list-nobody-keeps-correct that this whole file exists to avoid.
    """
    kw = {k.arg: k.value for k in _health_call().keywords if k.arg}
    value = kw[field]
    assert not isinstance(value, ast.Constant), (
        f"`{field}` is passed as the literal {value.value!r}, so /health reports a constant while "
        f"looking wired — the engine could publish anything and this would not move"
    )
    empty_container = isinstance(value, (ast.List, ast.Dict, ast.Set, ast.Tuple)) and not (
        getattr(value, "elts", None) or getattr(value, "keys", None)
    )
    assert not empty_container, (
        f"`{field}` is passed as an empty literal container — `0 of 0` where the engine may be "
        f"saying `0 of 4`"
    )


# ==================================================================================================
# The MIDDLE hop — `consumer.health()`, which had no guard at all
# ==================================================================================================
#: Model fields the consumer is not expected to source from the bus frame: the api derives or probes
#: them itself, or they are image stamps from the environment.
NOT_FROM_THE_BUS = NOT_FROM_THE_ENGINE | {
    "cockpit_sha", "cockpit_digest", "strategies_sha", "strategies_digest",  # env / provenance
    "feed_last_tick_ts",      # carried as `last_tick_ts`, renamed at the endpoint
    "inert",                  # computed by the api from the observation
    "ownership_violations",   # computed by the api from `inert`
    "feed_stale",             # derived by the api from last_tick_ts + calendar
    # Computed by the api from `node.positions()` + the Postgres claims ledger (#817). The engine
    # does not own the claims half and has no business publishing this. It stays OUT of
    # NOT_FROM_THE_ENGINE, so the construction guard above still requires /health to pass it.
    "split_divergence",
    # Computed by the api from the journal (#1098) — both processes share Postgres; the engine
    # publishes nothing for it. Same reasoning as `split_divergence` directly above.
    "lanes_bleeding",
}


def _consumer_health_keys() -> set[str]:
    import api.consumer as consumer_mod

    fn = consumer_mod.RedisConsumer.health
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys |= {k.value for k in node.keys if isinstance(k, ast.Constant)}
    return keys


def test_the_CONSUMER_hop_forwards_every_engine_owned_field():
    """The hop that silently ate two shipped fields.

    Not a list of names — derived from the model, so a field added tomorrow is covered here too. If
    a new field genuinely does not come off the bus, add it to NOT_FROM_THE_BUS *with a reason*; the
    point is that the omission has to be a decision someone wrote down rather than an oversight.
    """
    expected = set(HealthResponse.model_fields) - NOT_FROM_THE_BUS
    missing = sorted(expected - _consumer_health_keys())
    assert not missing, (
        f"{missing} never leave `consumer.health()`, so /health reports them empty however correct "
        f"the engine is. This is the same field-list trap as the endpoint, one hop earlier, and it "
        f"is where `unpriced_positions` and `failed_requests` died on both live stacks."
    )


def test_the_forwarded_fields_are_GATED_ON_THE_BRIDGE():
    """A stale frame must not report a live picture.

    Every sibling in that dict is `... if bridge_ok else <empty>`, deliberately: a dead engine's last
    known subscription state is not the current one, and an unpriceable book from a frame nobody is
    refreshing would read as a measurement taken now.
    """
    import api.consumer as consumer_mod

    # ON THE VALUE NODE, not on the rendered text. The first version searched the unparsed source
    # for a line containing both the key and `bridge_ok` — and `ast.unparse` puts the whole dict on
    # ONE line, so every sibling's gate vouched for the one under test. Removing the gate left it
    # green. Agreement is not connection, inside the test this time.
    tree = ast.parse(textwrap.dedent(inspect.getsource(consumer_mod.RedisConsumer.health)))
    values = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant):
                    values[k.value] = v
    for field in ("unpriced_positions", "failed_requests", "subscriptions"):
        value = values.get(field)
        assert value is not None, f"{field} is not forwarded by the consumer at all"
        assert isinstance(value, ast.IfExp) and "bridge_ok" in ast.unparse(value.test), (
            f"`{field}` is forwarded as {ast.unparse(value)[:70]!r} with no bridge gate, so a dead "
            f"engine's last frame keeps reporting as though it were current"
        )


# ==================================================================================================
# The gap that let a FIFTH field through — engine keys that are not model fields
# ==================================================================================================
#: Keys the engine publishes for its own use, not for `/health`. Each needs a written reason; the
#: point is that an omission is a decision someone made, not an oversight nobody saw.
ENGINE_INTERNAL = {
    "build",                 # image stamp, reported through _provenance instead
    "ts",                    # frame timestamp, used for staleness by the consumer itself
    "engine_ok",             # consumed by the consumer to compute bridge_ok
    "last_tick_ts",          # renamed to feed_last_tick_ts at the endpoint
    "unreconciled_orders",   # consumed by the alerts service, not by /health
}


def _engine_payload_keys() -> set[str]:
    """The literal keys the engine publishes in its health frame."""
    import api.engine_node as en

    src = inspect.getsource(en)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        # The health payload is the one carrying these three together.
        if {"failed_requests", "subscriptions", "book_truth"} <= keys:
            return keys
    raise AssertionError("could not locate the engine's health payload literal")


def test_the_fixture_can_find_the_engine_payload():
    """Vacuity guard: if the payload cannot be located, the test below asserts nothing at all."""
    assert len(_engine_payload_keys()) > 5


def test_EVERY_KEY_THE_ENGINE_PUBLISHES_reaches_the_consumer_or_is_declared_internal():
    """The guard derived from `HealthResponse.model_fields` could not catch `observations`, because
    it was not a model field yet — so a field published by the engine and declared nowhere fell
    straight through all three hops for the FIFTH time, inside the commit about that very trap.

    Deriving from the ENGINE side closes it: a new key is covered the moment it is published.
    """
    import api.consumer as consumer_mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(consumer_mod.RedisConsumer.health)))
    forwarded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            forwarded |= {k.value for k in node.keys if isinstance(k, ast.Constant)}

    missing = sorted(_engine_payload_keys() - forwarded - ENGINE_INTERNAL)
    assert not missing, (
        f"{missing} are published by the engine and neither forwarded by the consumer nor declared "
        f"ENGINE_INTERNAL. They report into a void. Add the forward, or add the name here WITH a "
        f"reason — four fields died in this exact gap on 2026-08-31."
    )


# ==================================================================================================
# The gap the model-derived guard CANNOT see: forwarded by the consumer, absent from the model
# ==================================================================================================
def test_EVERY_KEY_THE_CONSUMER_FORWARDS_reaches_the_model_or_is_declared():
    """Measured 2026-09-01: `armed_lanes` and `next_fire_ns` are published by the engine, forwarded
    by `RedisConsumer.health()`, and are NOT HealthResponse fields — so `/health` never carries them
    and every reader gets `None`, indistinguishable from "no lane is armed".

    That cost real time: minutes before an armed slot I nearly restarted the engine because the
    payload said the lanes were not armed. They were, and one decided two minutes later. The payload's
    blindness nearly caused the outage it appeared to report.

    THE GUARD ABOVE COULD NOT SEE IT. It derives the expected set from `HealthResponse.model_fields`,
    so a key that never reached the model is invisible to it — a detector that recognises its subject
    by a property this defect destroys. This one derives from the CONSUMER instead, which is the
    layer that has already decided the key is worth carrying.
    """
    import api.consumer as consumer_mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(consumer_mod.RedisConsumer.health)))
    forwarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            forwarded |= {k.value for k in node.keys if isinstance(k, ast.Constant)}

    #: Keys the consumer carries for a reader OTHER than `/health`, each with the reason.
    FOR_OTHER_READERS = {
        "bridge_ok",            # the freshness signal itself; `/health` expresses it via `status`
        "engine_ok",            # folded into the subsystem probe
        "last_tick_ts",         # renamed `feed_last_tick_ts` at the endpoint
        "reconcile_drift",      # consumed by the drift banner and the alerts service
        "unreconciled_orders",  # consumed by the alerts service (#643)
    }

    missing = sorted(forwarded - set(HealthResponse.model_fields) - FOR_OTHER_READERS)
    assert not missing, (
        f"{missing} are forwarded by the consumer and are not HealthResponse fields, so `/health` "
        f"answers None for them however correct the engine is. Add the field, or list it above WITH "
        f"the reader it exists for."
    )
