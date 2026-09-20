"""A claim the cache contradicts must reach `/health`, not only a Telegram alert (#817).

MEASURED on ibkr-paper, 2026-09-09 19:10 SGT, by running `api/split_divergence.py::split_divergence`
against the live position book and the live `exec_position_state`:

    DIVERGENT PAIRS: 11        (BCTROT-004, every one)
      AEM 46/0   AMGN 22/0   ARKK 115/0   AYA 360/0   CGAU 428/0   GLD 23/0
      HALO 93/0  LH 29/0     NSIT 64/0    SSRM 262/0  WPM 62/0
    1,504 shares claimed by a lane the cache does not attribute to it.

The detector is correct and finds all eleven. It has exactly one caller,
`AlertsService._announce_split_divergence` (alerts.py:643), and that caller sits inside
`if self._enabled():` (alerts.py:1043), which reads `notifications.enabled` — FALSE on ibkr-paper. One
boolean, the TELEGRAM DELIVERY toggle, decides whether the system LOOKS. An instance with no Telegram
credential cannot observe itself and its silence is indistinguishable from health.

The gate is right as a rule about SENDING; its comment cites a codex review, "default off has to mean
off, not does the work then declines to send". What is wrong is that DETECTION rides on it.

GO-LIVE BLOCKER, not a reporting nicety: #813's calendar fix arms BCTROT-004, whose exits size
against the claims ledger — 1,504 shares the engine attributes to EXTERNAL. Under NETTING that sell
comes out of another owner's position. #692 is the same shape at a eleventh of the size, and records
that only a resting cross-lane stop blocked it.

`book_truth.split_disagrees` reads 0 while these eleven stand. Different comparison (engine vs venue);
reading it as this one is worse than silence, because it looks like an answer.

SCOPE. Split divergence only. Five other checks sit behind the same gate (external positions, short
violations, stranded claims, never-ran, pool-unlisted, slots) — codex's review notes that `slots` and
`never_ran` are DELIBERATELY gated, pinned by `test_slot_detector_is_wired.py:76` and
`test_never_ran.py:114`, and that several others already have their own endpoint. Turning this file
into a policy test over all six would contradict those. Separate ticket.
"""

from __future__ import annotations

import ast
import inspect

import pytest

import api.app as app_mod
from api.models import HealthResponse

class _Row:
    """A SQLAlchemy-shaped row: production reads `r._mapping`, not tuple unpacking (alerts.py:663).

    Codex caught the first draft passing plain tuples — looser than production, so an implementation
    that only handled tuples would have passed while failing on the real query. `build_breaches`
    models the same access pattern (claims_endpoint.py:47).
    """

    def __init__(self, strategy_id, symbol, qty):
        self._mapping = {"strategy_id": strategy_id, "symbol": symbol, "qty": qty}


#: THE CACHE SIDE, in the shape `/positions` and the alert both see: UNSIGNED `quantity` with the
#: direction in `side`, and a VENUE-SUFFIXED instrument id against a BARE claim symbol.
#:
#: BCTROT holds a PARTIAL 10 of AEM. That is what makes the suffix case able to fail: codex showed
#: that with the claimed symbols held only by EXTERNAL, an implementation that never strips
#: `AEM.XNYS -> AEM` still computes BCTROT's cache as 0.0 and passes for the wrong reason. Now a
#: broken strip gives 0.0 where 10.0 is correct, and the assertion moves.
_CACHE_ROWS = [
    {"strategy_id": "EXTERNAL", "instrument_id": "AEM.XNYS", "quantity": 36.0, "side": "LONG"},
    {"strategy_id": "BCTROT-004", "instrument_id": "AEM.XNYS", "quantity": 10.0, "side": "LONG"},
    {"strategy_id": "BCTROT-004", "instrument_id": "ARKK.BATS", "quantity": 115.0, "side": "LONG"},
    # A SHORT the claims ledger DOES claim — signed, so an implementation ignoring `side` reads
    # -20 as +20 and reports agreement where the lane is mirrored the other way.
    {"strategy_id": "BCTROT-004", "instrument_id": "WHD.XNYS", "quantity": 20.0, "side": "SHORT"},
    # A FLAT row: `split_divergence` treats it as 0 explicitly (split_divergence.py:29), and without
    # one nothing here exercises that branch.
    {"strategy_id": "BCTROT-004", "instrument_id": "LH.XNYS", "quantity": 0.0, "side": "FLAT"},
    # Held with no claim at all — the adoption path, which must NOT be reported.
    {"strategy_id": "MANUAL-001", "instrument_id": "NTCT.XNAS", "quantity": 56.0, "side": "SHORT"},
]

#: ARKK agrees (115 == 115) so the clean assertions are about a computed match rather than an empty
#: input — codex: "empty claims prove 'nothing asked', not 'computed and no divergence'".
_CLAIM_ROWS = [
    _Row("BCTROT-004", "AEM", 46.0),     # cache attributes 10 -> DIVERGES
    _Row("BCTROT-004", "ARKK", 115.0),   # cache attributes 115 -> agrees
    _Row("BCTROT-004", "WHD", 20.0),     # cache attributes -20 -> DIVERGES on sign
    _Row("BCTROT-004", "LH", 29.0),      # cache FLAT -> DIVERGES
]


def _build():
    from api.split_divergence import build_split
    return build_split


# ---------------------------------------------------------------------------------------------
# FIXTURE PROPERTIES. Each one, if false, makes an assertion below pass against a broken surface.
# ---------------------------------------------------------------------------------------------

def test_the_fixture_diverges_on_THREE_DISTINCT_MECHANISMS_and_agrees_on_one():
    """Partial attribution, sign, and FLAT — plus one genuine agreement. A fixture that diverged for
    only one reason could not tell a correct implementation from one that gets that reason right."""
    from api.split_divergence import split_divergence
    claims = {"BCTROT-004": {r._mapping["symbol"]: r._mapping["qty"] for r in _CLAIM_ROWS}}
    d = split_divergence(_CACHE_ROWS, claims)
    assert set(d) == {"AEM", "WHD", "LH"}, f"expected three mechanisms to diverge, got {sorted(d)}"
    assert "ARKK" not in d, "the agreeing pair was reported as divergent"
    assert d["AEM"]["BCTROT-004"]["cache"] == 10.0, (
        "the venue suffix was not stripped, so AEM reads 0.0 and the suffix case cannot fail"
    )
    assert d["WHD"]["BCTROT-004"]["cache"] == -20.0, "the SHORT was not signed"


def test_the_fixture_carries_a_HELD_position_with_NO_claim_that_must_not_be_reported():
    """`split_divergence` is deliberately one-sided — a cache position with no claim is the adoption
    path, not a defect. Without such a row nothing distinguishes a correct detector from one that
    flags every unattributed position, which on ibkr-paper would be all 17."""
    from api.split_divergence import split_divergence
    assert split_divergence(_CACHE_ROWS, {}) == {}


def test_notifications_are_OFF_in_this_test_environment():
    """The precondition the whole file rests on. If alerts were enabled here, the behavioural test
    below would pass whether or not detection was decoupled from delivery."""
    from api import settings
    assert not (settings.resolve("notifications") or {}).get("enabled"), (
        "notifications are enabled in the test env — the /health test below proves nothing"
    )


# ---------------------------------------------------------------------------------------------
# THREE STATES. "computed, none found" is not "could not compute".
# ---------------------------------------------------------------------------------------------

def test_an_AGREEING_book_reports_ok_with_an_empty_list():
    agreeing = [_Row("BCTROT-004", "ARKK", 115.0)]
    r = _build()(_CACHE_ROWS, agreeing)
    assert (r.status, r.pairs, r.error) == ("ok", [], None)


def test_the_divergent_book_reports_EVERY_pair():
    r = _build()(_CACHE_ROWS, _CLAIM_ROWS)
    assert r.status == "ok"
    got = {(p["symbol"], p["strategy_id"], p["claim"], p["cache"]) for p in r.pairs}
    assert got == {
        ("AEM", "BCTROT-004", 46.0, 10.0),
        ("WHD", "BCTROT-004", 20.0, -20.0),
        ("LH", "BCTROT-004", 29.0, 0.0),
    }, got


@pytest.mark.parametrize(("cache_rows", "claim_rows", "who"),
                         [(None, _CLAIM_ROWS, "cache"), (_CACHE_ROWS, None, "claims")])
def test_an_UNREADABLE_side_is_its_own_STATUS_and_never_an_empty_list(cache_rows, claim_rows, who):
    """"A check that cannot see the account has not found zero breaches, it has found nothing"
    (claims_endpoint.py). `[]` with status ok is a claim of health about a book nobody read."""
    r = _build()(cache_rows, claim_rows)
    assert r.status != "ok", f"an unreadable {who} reported ok"
    assert r.pairs == []
    assert r.error and who in r.error.lower(), f"the error does not name the half that failed: {r.error!r}"


# ---------------------------------------------------------------------------------------------
# WIRING PROPERTIES a behavioural test cannot see.
# ---------------------------------------------------------------------------------------------

def test_the_HEALTH_MODEL_declares_the_field():
    assert "split_divergence" in HealthResponse.model_fields, (
        "HealthResponse cannot carry split divergence, so /health can never report it"
    )


def test_the_field_is_NOT_EXEMPTED_from_the_generic_forwarding_guard():
    """NOT a second copy of that guard — a guard ON it.

    `test_health_forwards_every_engine_field.py` already requires every `HealthResponse` field to be
    NAMED in the construction and not wired to a constant, so asserting that here again would be a
    second derivation of one rule, which drifts. Codex flagged exactly that redundancy.

    What it does NOT protect is its own exemption list: adding `split_divergence` to
    `NOT_FROM_THE_ENGINE` would silence it for this field with a one-line edit that reads like
    bookkeeping. `ownership_violations` is API-computed too and is deliberately NOT exempted — that
    file's own docstring explains it reaches the construction "by another route" and is checked
    anyway. This field belongs in the same place.
    """
    import api.test_health_forwards_every_engine_field as guard

    assert "split_divergence" not in guard.NOT_FROM_THE_ENGINE, (
        "split_divergence was exempted from the forwarding guard. It is API-computed, like "
        "ownership_violations, which is NOT exempted — being computed here is not a reason to stop "
        "checking that it is forwarded (#546)."
    )
    assert "split_divergence" in guard._engine_owned_fields(), (
        "the generic guard no longer covers this field, so nothing checks that /health passes it"
    )
    # THE OTHER LIST, AND IT IS A DIFFERENT QUESTION (codex, scope review). `NOT_FROM_THE_BUS` is
    # for fields the engine does NOT publish over the Redis bus. This one is computed in the api
    # from `node.positions()` plus Postgres — exactly like `ownership_violations`, which sits there
    # with its reason written down. Omitting it would make the bus guard demand a key the engine has
    # no business sending.
    assert "split_divergence" in guard.NOT_FROM_THE_BUS, (
        "split_divergence is not declared bus-exempt, so the consumer guard will require the engine "
        "to publish a key it does not own"
    )


def test_a_DIVERGENT_BOOK_DEGRADES_the_overall_health_status():
    """CODEX, SCOPE REVIEW: a green banner over eleven divergent pairs is wrong.

    `/health`'s `status` is already not subsystems-only — it degrades on unpriced positions and on a
    stale feed (app.py:504), and `test_unpriced_book_is_reported.py:99` states the rule those follow:
    a book that cannot be SIZED from is not `ok`. Split divergence is exit-sizing unsafe by exactly
    the same argument — it is the condition under which a lane sells shares it does not hold.

    Asserted at the source because the behavioural half needs the engine and lives in test_app.py.
    """
    src = inspect.getsource(app_mod.health)
    tree = ast.parse(src.lstrip())
    call = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "HealthResponse"
    )
    status = next(k.value for k in call.keywords if k.arg == "status")
    rendered = ast.unparse(status)
    assert "split" in rendered, (
        f"the overall status does not consider split divergence, so a divergent book shows a green "
        f"banner while an exit sized off it sells another owner's shares. status={rendered!r}"
    )


def test_THE_HEALTH_PATH_never_consults_the_notifications_toggle():
    """THE DEFECT ITSELF, asserted where it actually lives.

    The first draft forbade these words inside `api.split_divergence`, which codex correctly called
    the wrong place — `/health` could call `app.state.alerts._enabled()` and that test would still
    pass. The gate is `alerts.py:260` / `alerts.py:1043`; what must not reach it is the health path.

    AST, NOT A SUBSTRING, and the first version of THIS test proved why: it failed on its own
    subject's docstring, which explains the defect and therefore contains the word "notifications".
    A grep cannot tell a reference from an explanation, and the explanation is the part worth
    keeping. Same lesson as `_env_names_read_by` in test_api_process_env_is_forwarded.py.
    """
    def _names_used(fn):
        """Every attribute name and called name in a function, ignoring strings and docstrings."""
        tree = ast.parse(inspect.getsource(fn).lstrip())
        out: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute):
                out.add(n.attr)
            elif isinstance(n, ast.Name):
                out.add(n.id)
        return out

    used: set[str] = set(_names_used(app_mod.health))
    for helper in ("_split_divergence", "_claim_rows"):
        fn = getattr(app_mod, helper, None)
        if fn is not None:
            used |= _names_used(fn)

    for forbidden in ("_enabled", "alerts", "AlertsService", "Notifier"):
        assert forbidden not in used, (
            f"the /health split-divergence path USES {forbidden!r} — detection is coupled to "
            f"delivery again, which is what made eleven divergent pairs invisible"
        )
    # The settings domain itself, reached by name rather than by attribute.
    calls = [
        n for fn in (app_mod.health, getattr(app_mod, "_split_divergence", None))
        if fn is not None
        for n in ast.walk(ast.parse(inspect.getsource(fn).lstrip()))
        if isinstance(n, ast.Call)
    ]
    for c in calls:
        args = [a.value for a in c.args if isinstance(a, ast.Constant)]
        assert "notifications" not in args, (
            "the health path resolves the `notifications` settings domain — the delivery toggle "
            "must not decide whether detection runs"
        )


def test_HEALTH_DOES_NOT_INHERIT_THE_ALERTS_DEBOUNCE():
    """The alert waits two consecutive polls before paging, because claims are written asynchronously
    around fills and a poll landing mid-update sees a self-healing blip (alerts.py:672). A HEALTH
    surface must report current truth on the first read — copying the debounce here would make the
    page lie for one interval, and an operator refreshing after a deploy is exactly who reads it."""
    src = inspect.getsource(app_mod.health)
    for helper in ("_split_divergence", "_claim_rows"):
        fn = getattr(app_mod, helper, None)
        if fn is not None:
            src += inspect.getsource(fn)
    for forbidden in ("_split_pending", "_split_alarmed", "debounce"):
        assert forbidden not in src, f"the health path carries the alert's debounce state ({forbidden})"


def test_the_ALERT_and_HEALTH_call_THE_SAME_FUNCTION():
    """Two derivations of one fact drift, and `/health` says so about `ownership_violations`: computed
    "from the same call the alert makes ... so the banner and the page cannot disagree".

    Asserted by IDENTITY, not by string matching — codex showed the first draft's substring check
    would pass on an import, a docstring or a dead assignment.
    """
    import api.alerts as alerts_mod
    import api.split_divergence as sd_mod

    alert_names = set(inspect.getsource(alerts_mod.AlertsService._announce_split_divergence).split())
    assert any("split_divergence" in n for n in alert_names), "the alert no longer uses the detector"

    build = getattr(sd_mod, "build_split", None)
    assert build is not None
    called = ast.unparse(ast.parse(inspect.getsource(app_mod.health).lstrip()))
    helper_src = "".join(
        inspect.getsource(getattr(app_mod, h)) for h in ("_split_divergence", "_claim_rows")
        if getattr(app_mod, h, None) is not None
    )
    assert "build_split" in called + helper_src, (
        "/health does not reach api.split_divergence.build_split — it derives the answer some other "
        "way, and two derivations of one fact drift"
    )
