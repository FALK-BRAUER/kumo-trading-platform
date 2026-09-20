"""Operator alerts (#197). Settings decide WHETHER, `telegram.py` decides HOW.

Why this exists: some conditions cannot be seen from the cockpit at all. A strategy that hits an exit
rule the live runner cannot honour suppresses new entries and keeps exiting — and the lifecycle still
reads TRADING, because there is no state for "entering nothing". The book drifts to cash and the only
record is a journal row. The operator is not watching a screen at 21:35 SGT; the phone is the surface that
actually reaches him.

Two rules shape everything here:

  NOTIFICATION MUST NOT AFFECT TRADING. Every path swallows its errors and returns a bool. A notifier
  that can raise into a session is a liability, not a safety feature.

  A REPEATING CONDITION IS NOT A REPEATING ALERT. Drift is republished every reconcile cycle and the
  engine-down check runs on a poll; sending each one would train the operator to mute the channel,
  which costs more than never having built it. `Notifier` suppresses a repeat of the same key until
  it clears, so an alert means "this started" and silence means "nothing changed".
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from datetime import datetime

from api.notify.telegram import SGT, Alert, TelegramNotifier

_log = logging.getLogger(__name__)

__all__ = ["Alert", "Notifier", "TelegramNotifier"]

#: alert-key PREFIX (everything before the first ":") -> the settings flag that gates it.
#: A prefix absent from here is always allowed, so a new alert kind is heard by default rather than
#: silently dropped — but see `test_every_alert_prefix_is_gated`, which requires the decision to be
#: explicit rather than accidental.
#:
#: These MUST be the prefixes `api/alerts.py` actually emits. The first version of this table used
#: the settings-flag names (`reconcile_drift`, `engine_down`) while the emitter used `drift:SYM` and
#: `subsystem_down:NAME` — so not one switch was connected, turning `notify_reconcile_drift` off did
#: nothing, and the unit test passed because it used a key production never produces.
_GATES = {
    "subsystem_down": "notify_engine_down",
    "drift": "notify_reconcile_drift",
    "unreconciled_order": "notify_unreconciled_order",
    "split_divergence": "notify_split_divergence",
    "external_position": "notify_external_position",
    "short_violation": "notify_short_violation",
    "terminal_fill": "notify_terminal_fill",
    "stranded_claim": "notify_stranded_claim",
    "strategy_degraded": "notify_strategy_degraded",
    "pool_unlisted": "notify_pool_unlisted",
    "digest": "notify_daily_digest",
    # #873: a lane's market view / self-assessment TRANSITIONS. Default TRUE in the schema: an
    # OBSERVATION flag, not an automation gate — nothing behind it can place an order.
    "market_aware": "notify_market_aware",
    # #1098: a lane going to cash under a TRADING label. Default TRUE: an OBSERVATION flag — nothing
    # behind it can place an order, and the condition is the one every surface said was healthy.
    "lanes_bleeding": "notify_lanes_bleeding",
}


class Notifier:
    """Settings-aware, deduplicating front end over a transport.

    `key` identifies the CONDITION, not the message — "engine_down", "drift:HSBC". The same key fires
    once until `clear(key)` or a `send` reporting it resolved. Callers can therefore poll as often as
    they like and let this decide what is news.
    """

    async def _already_announced(self, key: str, ttl_hours: float) -> bool:
        """True if this condition was announced recently enough to stay quiet about.

        DURABLE, because the in-memory set alone re-announced everything on every API restart — two
        identical HSBC drift alerts three minutes apart during a routine redeploy, which is precisely
        how a channel earns a mute. Redis is already here, is AOF-backed, and has native TTL.

        The TTL, rather than a permanent mark, is the point: a condition still true tomorrow is worth
        repeating once, and nothing has to remember to clean up after a condition that resolves while
        the API is down.

        Falls back to the in-memory set when Redis is unreachable — a dedupe store that is itself a
        dependency must not be able to silence alerts. Note the fallback direction: on Redis failure
        we would rather send twice than not at all, and Redis being down is itself an alert.
        """
        client = await self._redis()
        if client is None:
            return self._memory_announced(key)
        try:
            # SET NX EX: returns truthy only when the key did NOT already exist, so this is
            # check-and-mark in one round trip with no race between two API instances.
            fresh = await client.set(f"kumo:alert:{key}", "1", nx=True, ex=int(ttl_hours * 3600))
            return not fresh
        except Exception as exc:                                        # noqa: BLE001
            _log.warning("alert dedupe unavailable (%r) — falling back to in-memory", exc)
            return self._memory_announced(key)

    def _memory_announced(self, key: str) -> bool:
        """The in-memory mark, with the SAME 15-minute retry-after-failure the redis TTL gives.

        The plain `key in self._active` check had no failure TTL at all, so one transport blip
        silenced the key for the LIFE OF THE PROCESS — for a dated key like the daily digest, that is
        permanent loss wearing the dedupe's clothes (#651 item 3). A key whose last send FAILED ages
        out after `_RETRY_AFTER_FAILURE_S`, exactly like `_shorten` arranges on redis; a key whose
        send succeeded holds the mark as before.
        """
        if key not in self._active:
            return False
        failed_at = self._failed_at.get(key)
        if failed_at is not None and time.monotonic() - failed_at >= self._RETRY_AFTER_FAILURE_S:
            self._active.discard(key)
            self._failed_at.pop(key, None)
            return False
        return True

    async def _redis(self):
        if self._redis_client is not None:
            return self._redis_client
        if self._redis_disabled:
            return None
        try:
            import os

            import redis.asyncio as aioredis
            self._redis_client = aioredis.Redis(
                host=os.environ.get("KUMO_REDIS_HOST", "127.0.0.1"),
                port=int(os.environ.get("KUMO_REDIS_PORT", 6379)),
                socket_connect_timeout=2.0, socket_timeout=2.0)
            return self._redis_client
        except Exception as exc:                                        # noqa: BLE001
            _log.warning("alert dedupe: no redis (%r) — in-memory only", exc)
            self._redis_disabled = True
            return None

    def __init__(self, transport: TelegramNotifier | None = None, settings=None,
                 dedupe_backend: str = "redis") -> None:
        self._redis_client = None
        self._redis_disabled = dedupe_backend != "redis"
        # An injected transport wins outright (tests, and any caller that wants a fixed account).
        # Otherwise it is built lazily from settings, because the account is a SETTING: changing it
        # in the UI must take effect without a restart, which a transport captured in __init__ would
        # not allow.
        self._tx = transport
        # Injected for tests; by default read the live settings store lazily, so importing this
        # module never touches the filesystem.
        self._settings = settings
        self._active: set[str] = set()
        #: key -> time.monotonic() of its last FAILED send; the in-memory analogue of the shortened
        #: redis TTL, so the memory fallback also retries after 15 minutes (#651 item 3).
        self._failed_at: dict[str, float] = {}

    def _transport(self, cfg: dict) -> TelegramNotifier:
        if self._tx is not None:
            return self._tx
        account = str(cfg.get("telegram_account") or "ledger_tool")
        return TelegramNotifier(account=account)

    def _config(self) -> dict:
        if self._settings is not None:
            return self._settings
        try:
            from api.settings import store
            return store.resolve("notifications")
        except Exception as exc:                                        # noqa: BLE001
            # An unreadable or invalid settings file must not silence alerts AND must not crash the
            # caller. Defaults have `enabled: false`, so this degrades to "no alerts" rather than to
            # "no engine".
            _log.warning("notification settings unreadable (%r) — treating as disabled", exc)
            return {}

    def allowed(self, key: str) -> bool:
        cfg = self._config()
        if not cfg.get("enabled"):
            return False
        gate = _GATES.get(key.split(":", 1)[0])
        return True if gate is None else bool(cfg.get(gate, True))

    def clear(self, key: str) -> None:
        """The condition resolved. The next occurrence is news again.

        Sync so callers in a diff loop stay simple; the durable key is dropped best-effort on the
        running loop. A clear that fails to reach Redis costs one suppressed repeat, not a lost
        alert, because the TTL expires it anyway.
        """
        self._active.discard(key)
        if self._redis_client is not None:
            import asyncio
            with suppress(RuntimeError):
                asyncio.get_running_loop().create_task(self._drop(key))

    async def _drop(self, key: str) -> None:
        try:
            await self._redis_client.delete(f"kumo:alert:{key}")
        except Exception as exc:                                        # noqa: BLE001
            _log.debug("alert dedupe clear failed for %s: %r", key, exc)

    async def send(self, key: str, alert: Alert, *, now: datetime | None = None) -> bool:
        if not self.allowed(key):
            return False
        cfg = self._config()
        if await self._already_announced(key, float(cfg.get("repeat_after_hours", 12))):
            return False                    # already announced; still true is not news
        now = now or datetime.now(SGT)
        # NO QUIET HOURS. Removed 2026-08-16 — the defaults silenced alerts across the entire US
        # session. Regular hours are 21:30-04:00 SGT and the default window was 22:00-07:59 SGT, so
        # every non-critical alert arrived muted during exactly the hours it mattered, and only the
        # ones outside trading ever buzzed. Operator: "I can set my phone quiet if i want."
        #
        # An app deciding when a trading alert is worth waking someone for is guessing at something
        # the phone already knows, and getting it wrong here is silent by construction — a muted
        # alert looks identical to no alert.
        ok = await self._transport(cfg).send(alert, silent=False)
        self._active.add(key)
        if not ok:
            # A FAILED send must not hold the full repeat window. Marking on attempt was right while
            # dedupe was in-memory and short-lived; with a durable 12h TTL it means one Telegram
            # blip silences a real condition for half a day (codex review). Shorten the mark instead:
            # long enough that a transport outage cannot retry every 30s and burst on recovery, short
            # enough that the alert is genuinely retried.
            await self._shorten(key)
            # The in-memory mirror of that shortening — see `_memory_announced` (#651 item 3).
            self._failed_at[key] = time.monotonic()
        else:
            self._failed_at.pop(key, None)
        return ok

    _RETRY_AFTER_FAILURE_S = 900
    """15 minutes: ~30 poll intervals, so no burst, and a real condition is re-attempted within the
    hour rather than at the end of the repeat window."""

    async def _shorten(self, key: str) -> None:
        if self._redis_client is None:
            return
        try:
            await self._redis_client.expire(f"kumo:alert:{key}", self._RETRY_AFTER_FAILURE_S)
        except Exception as exc:                                        # noqa: BLE001
            _log.debug("could not shorten dedupe ttl for %s: %r", key, exc)
