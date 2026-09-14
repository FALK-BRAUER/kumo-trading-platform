"""Turn a finished strategy session into an operator alert (#199 A).

Runs in the ENGINE process, because that is where sessions happen. The API process cannot see a
session result — the two do not share memory — so the health/drift/digest alerts in `api/alerts.py`
live there and this lives here. Both use the same `Notifier`, so settings, quiet hours and the
durable dedupe apply identically.

The condition this exists for: a strategy configured with an exit rule the live runner cannot honour
suppresses new entries and keeps exiting, while the lifecycle still reads TRADING — there is no state
meaning "entering nothing", and full liquidation is deliberately an operator action in the broker app
rather than something the engine automates. So the book drifts to cash with nothing on any screen
saying so.

Deliberately narrow. A session that decides nothing, or decides normally, is not an alert — it is the
daily digest's job to report those, and an alert per session would train the operator to mute the
channel, which costs more than never having built it.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def build_session_observer(strategy_id: str):
    """An `on_result` callback for `SessionGateway`, or None when alerting is unavailable.

    IDENTITY IS EXPLICIT — no default (#651 item 1). `_build_rotation` builds every rotation lane, so
    a `strategy_id="MOMENTUM-002"` default meant BCTROT-004's "stopped entering" alert named
    MOMENTUM-002, shared its 12h dedupe key (one lane's alert suppressing the other's), and BCTROT
    entering cleared MOMENTUM's alarm. Same defect, same fix as `PgJournal` (kumo-strategies#54).

    Returns a coroutine function so the gateway can await it. Never raises out: the gateway already
    guards, and belt-and-braces here keeps a notifier import problem from reaching a trading path.
    """
    try:
        from api.notify import Alert, Notifier
    except Exception as exc:                                            # noqa: BLE001
        _log.warning("session alerts unavailable (%r) — sessions will run unobserved", exc)
        return None

    notifier = Notifier()

    async def observe(result) -> None:
        detail = getattr(result, "detail", None) or {}
        suppressed = detail.get("suppressed_entries") or []
        unsupported = detail.get("unsupported_exits") or []

        if suppressed or unsupported:
            # Keyed per strategy, not per session: the condition persists until the config is fixed,
            # and the durable dedupe should suppress the repeat rather than alert every morning.
            rules = ", ".join(unsupported) or "an unimplemented exit rule"
            body = (f"*{strategy_id}* stopped entering.\n\n"
                    f"Configured but not implemented live: `{rules}`.\n"
                    f"Exits are still running — {len(suppressed)} planned entr"
                    f"{'y was' if len(suppressed) == 1 else 'ies were'} suppressed this session.\n\n"
                    f"The lifecycle still reads TRADING. The book will drift to cash until the "
                    f"config is changed or the rule is implemented.")
            await notifier.send(f"strategy_degraded:{strategy_id}",
                                Alert(title=f"{strategy_id} stopped entering", body=body))
            return

        # Entering again: the condition has resolved, so make the next occurrence news.
        if getattr(result, "entered", None):
            notifier.clear(f"strategy_degraded:{strategy_id}")

    return observe
