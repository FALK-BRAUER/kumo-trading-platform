"""Both detectors I shipped today are inert in production, and my own fail-soft hid it.

    never-ran check failed (other alerts unaffected): AttributeError("'str' object has no attribute 'critical'")

`Notifier.send(key, alert)` takes an `Alert(title, body, critical)`. `_announce_slots` (#476) and
`_announce_never_ran` (#477) both passed an f-STRING. Every send raises, the surrounding `except`
swallows it, and the alarm never fires. Deployed twice, an hour apart, and found only by reading the
api log after the second deploy.

THIS IS THE RULE I WROTE ABOUT EARLIER TONIGHT, BROKEN BY ME, HOURS LATER. CLAUDE.md: "Build the
double from what production ACTUALLY emits, then check it ... make the double REJECT what production
rejects." My wiring tests asserted the source CONTAINS `_announce_slots`, and my failure test set
`_http = None` so it returned before reaching `send`. Nothing ever called `send`. The double could not
be wrong because it was never used.

WHY THE FAIL-SOFT MADE IT WORSE. Both `except Exception` blocks exist so a broken check cannot take
down health and the digests, and that reasoning is still right. But a swallowed exception on EVERY
poll is indistinguishable from a clean poll, so "no alarms" read as "nothing wrong" — which is the
exact failure mode both detectors were built to end. A fail-soft that hides a permanently broken check
is not soft, it is off.

The `/slots` route was unaffected — it never touches the Notifier — so the data stayed reachable. That
is the only reason this was a dead push rather than a dead check.
"""

from __future__ import annotations

import asyncio

from api.notify.telegram import Alert


class _StrictNotifier:
    """REJECTS what the real `Notifier` rejects. The double is the test.

    The real `send` reaches `alert.critical` and `Transport.send(alert)`; a str has neither, so it
    raises AttributeError. Asserting the type here fails in the same place for the same reason,
    without a Telegram credential.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, Alert]] = []

    async def send(self, key: str, alert, **_):
        assert isinstance(alert, Alert), (
            f"Notifier.send takes an Alert, not {type(alert).__name__} — the real one reads "
            f".critical and hands it to the transport, so this raises in production"
        )
        assert isinstance(key, str) and key, "the dedupe key must be a non-empty string"
        self.sent.append((key, alert))
        return True


def _svc(notifier):
    from api import alerts

    s = alerts.AlertsService.__new__(alerts.AlertsService)
    s._n = notifier
    return s


def test_the_slot_scan_sends_an_ALERT_not_a_string() -> None:
    """#476's verdicts. Driven through the real `_announce_slots`, with a double that rejects a str."""
    from api.slot_outcome import SlotVerdict, Verdict

    n = _StrictNotifier()
    svc = _svc(n)
    v = SlotVerdict(Verdict.DECIDED_NEVER_ATTEMPTED, "dec 1 int 0 land 0 fail 0", may_be_benign=True)

    async def _scan():
        return [("QC345-003", "2026-08-21", "open+300m", v)]

    svc._scan_slots_impl = _scan
    asyncio.run(svc._announce_slots())
    assert len(n.sent) == 1, "the verdict never reached the notifier"
    key, alert = n.sent[0]
    assert "QC345-003" in key and "QC345-003" in (alert.title + alert.body)
    assert Verdict.DECIDED_NEVER_ATTEMPTED.value in (alert.title + alert.body)
    assert alert.critical is False, "a may-be-benign verdict must not wake anyone"


def test_a_NON_benign_verdict_is_critical() -> None:
    """`may_be_benign` is declared on the verdict precisely so this decision is not guesswork.
    ATTEMPTED, DID NOT LAND is a lane that formed orders and lost them — TECHIVOL's whole 08-21."""
    from api.slot_outcome import SlotVerdict, Verdict

    n = _StrictNotifier()
    svc = _svc(n)
    v = SlotVerdict(Verdict.ATTEMPTED_DID_NOT_LAND, "dec 1 int 8 land 0 fail 8", may_be_benign=False)

    async def _scan():
        return [("TECHIVOL-005", "2026-08-21", "open+150m", v)]

    svc._scan_slots_impl = _scan
    asyncio.run(svc._announce_slots())
    assert n.sent[0][1].critical is True


def test_the_never_ran_check_sends_an_ALERT_not_a_string(monkeypatch) -> None:
    """#477's. A lane that was due and wrote nothing is never benign — it is the silent outcome."""

    n = _StrictNotifier()
    svc = _svc(n)

    async def _missing():
        return [("QC345-003", "open+300m")]

    svc._never_ran_impl = _missing
    asyncio.run(svc._announce_never_ran())
    assert len(n.sent) == 1, "the missing slot never reached the notifier"
    key, alert = n.sent[0]
    assert "QC345-003" in key and "open+300m" in key
    assert alert.critical is True, "a lane that never ran is the outcome this exists to catch"
    assert "NEVER RAN" in (alert.title + alert.body).upper()


# ==================================================================================================
# A FAIL-SOFT THAT HIDES A PERMANENTLY BROKEN CHECK IS NOT SOFT, IT IS OFF.
#
# Both `except Exception` blocks exist so a broken check cannot take down health and the digests, and
# that reasoning is right. But a swallowed exception on EVERY poll is indistinguishable from a clean
# poll, so "no alarms" reads as "nothing wrong" — the exact failure both detectors were built to end.
# The f-string defect raised on every poll for an hour and the only trace was a log warning.
# ==================================================================================================

def test_a_check_that_fails_ONCE_stays_quiet() -> None:
    """A transient — a Postgres blip, a slow clock — must not page anyone."""

    n = _StrictNotifier()
    svc = _svc(n)
    svc._check_failures = {}
    svc._check_failed("never_ran", RuntimeError("blip"))
    assert n.sent == []


def test_a_check_that_KEEPS_failing_reports_ITSELF() -> None:
    """The defect this exists for. If it had been here, the f-string bug would have paged within two
    minutes instead of surviving two deploys."""
    from api import alerts

    n = _StrictNotifier()
    svc = _svc(n)
    svc._check_failures = {}
    for _ in range(alerts.BROKEN_CHECK_POLLS):
        svc._check_failed("never_ran", AttributeError("'str' object has no attribute 'critical'"))
    asyncio.run(svc._announce_broken_checks())
    assert len(n.sent) == 1, "a check broken on every poll never reported itself"
    key, alert = n.sent[0]
    assert "never_ran" in key
    assert alert.critical is True
    assert "critical" in alert.body, "the operator needs the actual exception, not just a name"


def test_a_RECOVERED_check_stops_reporting() -> None:
    """Or the alarm outlives the fault and teaches people to ignore it."""
    from api import alerts

    n = _StrictNotifier()
    svc = _svc(n)
    svc._check_failures = {}
    for _ in range(alerts.BROKEN_CHECK_POLLS):
        svc._check_failed("never_ran", RuntimeError("x"))
    svc._check_ok("never_ran")
    asyncio.run(svc._announce_broken_checks())
    assert n.sent == []
