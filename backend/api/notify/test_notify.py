"""Each test pins a property that would otherwise cost a missed alert or a muted channel.

Sync tests driving `asyncio.run`, per the repo convention (no pytest-asyncio) — see
scripts/test_export_bars.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pytest

from api.notify import Alert, Notifier
from api.notify.telegram import SGT, TelegramNotifier, env_keys


def _at(hour: int) -> datetime:
    return datetime(2026, 8, 9, hour, 30, tzinfo=SGT)


class FakeTx:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.sent = ok, []

    async def send(self, alert: Alert, *, silent: bool = False) -> bool:
        self.sent.append((alert, silent))
        return self.ok


ON = {"enabled": True}


# -- delivery is never silenced --------------------------------------------------------------------
#
# Quiet hours were REMOVED 2026-08-16. The defaults silenced alerts across the entire US session: US
# regular hours are 21:30-04:00 SGT and the default window was 22:00-07:59 SGT, so every non-critical
# alert arrived muted during exactly the hours it mattered, and only the ones outside trading ever
# buzzed. Operator: "I can set my phone quiet if i want."
#
# An application deciding when a trading alert is worth waking someone for is guessing at something
# the phone already knows, and getting it wrong is silent by construction — a muted alert is
# indistinguishable from no alert.


@pytest.mark.parametrize("hour", [3, 8, 12, 22, 23])
def test_an_alert_is_never_delivered_silently_whatever_the_hour(hour):
    """The property that replaced quiet hours: delivery does not depend on the clock.

    Parameterised across the boundaries of the window that used to exist — 3am was inside it, 22 and
    23 were its start, 8 was its end — so reintroducing any of it fails here rather than going
    unnoticed until an alert nobody heard.
    """
    tx = FakeTx()
    n = Notifier(transport=tx, dedupe_backend="memory", settings=ON)
    assert asyncio.run(n.send("reconcile_drift", Alert("drift", "HSBC"), now=_at(hour))) is True
    assert tx.sent[0][1] is False, f"an alert at {hour}:00 SGT was delivered silently"


def test_a_critical_alert_is_also_delivered_loudly():
    """Critical used to be the one thing that ignored quiet hours. With the window gone the
    distinction no longer affects delivery — kept as a test so a future 'quiet unless critical' does
    not creep back under a different name."""
    tx = FakeTx()
    n = Notifier(transport=tx, dedupe_backend="memory", settings=ON)
    asyncio.run(n.send("engine_down", Alert("engine down", "offline", critical=True), now=_at(3)))
    assert tx.sent[0][1] is False



def test_a_repeating_condition_alerts_once():
    """Drift republishes every reconcile cycle. Sending each one trains the operator to mute the
    channel, which costs more than never having built it."""
    tx = FakeTx()
    n = Notifier(transport=tx, dedupe_backend="memory", settings=ON)
    for _ in range(5):
        asyncio.run(n.send("drift:HSBC", Alert("drift", "HSBC -93"), now=_at(12)))
    assert len(tx.sent) == 1


def test_different_conditions_do_not_suppress_each_other():
    tx = FakeTx()
    n = Notifier(transport=tx, dedupe_backend="memory", settings=ON)
    asyncio.run(n.send("drift:HSBC", Alert("drift", "HSBC"), now=_at(12)))
    asyncio.run(n.send("drift:MET", Alert("drift", "MET"), now=_at(12)))
    assert len(tx.sent) == 2


def test_a_cleared_condition_is_news_again():
    tx = FakeTx()
    n = Notifier(transport=tx, dedupe_backend="memory", settings=ON)
    asyncio.run(n.send("drift:HSBC", Alert("drift", "x"), now=_at(12)))
    n.clear("drift:HSBC")
    asyncio.run(n.send("drift:HSBC", Alert("drift", "x"), now=_at(12)))
    assert len(tx.sent) == 2


def test_a_failed_send_does_not_retry_every_poll():
    """Marked active on attempt, not success — otherwise a transport outage produces a burst of
    duplicates the moment it recovers."""
    tx = FakeTx(ok=False)
    n = Notifier(transport=tx, dedupe_backend="memory", settings=ON)
    for _ in range(4):
        asyncio.run(n.send("engine_down", Alert("down", "x", critical=True), now=_at(12)))
    assert len(tx.sent) == 1


# -- gating --------------------------------------------------------------------------------------
def test_nothing_is_sent_while_disabled():
    """Default is off, per the rule that new automation gates start False."""
    tx = FakeTx()
    n = Notifier(transport=tx, dedupe_backend="memory", settings={"enabled": False})
    assert asyncio.run(n.send("engine_down", Alert("down", "x", critical=True))) is False
    assert tx.sent == []


def test_a_per_alert_switch_is_honoured():
    """Uses the keys `api/alerts.py` ACTUALLY emits — `drift:SYM`, `subsystem_down:NAME`.

    This test previously used `reconcile_drift` / `engine_down`, which are the settings-flag names and
    not keys anything sends. It passed while every switch in production was inert, because `_GATES`
    had drifted to the same fiction. `test_every_alert_prefix_is_gated` in api/test_alerts.py now
    binds the emitter to the table so the two cannot separate again.
    """
    tx = FakeTx()
    n = Notifier(transport=tx, dedupe_backend="memory", settings={**ON, "notify_reconcile_drift": False})
    asyncio.run(n.send("drift:HSBC", Alert("drift", "x"), now=_at(12)))
    asyncio.run(n.send("subsystem_down:engine", Alert("down", "x", critical=True), now=_at(12)))
    assert [a.title for a, _ in tx.sent] == ["down"]


def test_an_unknown_key_is_allowed_when_enabled():
    """A caller adding a new alert kind should not have to touch the schema to be heard at all."""
    tx = FakeTx()
    n = Notifier(transport=tx, dedupe_backend="memory", settings=ON)
    asyncio.run(n.send("something_new", Alert("new", "x"), now=_at(12)))
    assert len(tx.sent) == 1


def test_unreadable_settings_disable_alerts_rather_than_raising(monkeypatch):
    """A broken settings file must cost alerts, never the engine — the caller is a trading path."""
    from api.settings import store

    def boom(_domain):
        raise RuntimeError("corrupt settings file")

    monkeypatch.setattr(store, "resolve", boom)
    n = Notifier(transport=FakeTx(), dedupe_backend="memory")          # settings=None -> reads the live store
    assert n.allowed("engine_down") is False


# -- transport -----------------------------------------------------------------------------------
def test_an_unconfigured_transport_is_a_normal_state():
    """The cockpit runs on laptops and in CI with no credentials. Missing token is not an error."""
    assert TelegramNotifier(token="", chat_id="").configured is False
    assert TelegramNotifier(token="t", chat_id="c").configured is True


def test_an_unconfigured_transport_drops_quietly():
    assert asyncio.run(TelegramNotifier(token="", chat_id="").send(Alert("x", "y"))) is False


def test_an_unconfigured_DROP_IS_VISIBLE_not_debug(caplog):
    """THE BUG THIS PINS (2026-08-21).

    `notifications.enabled` was true, `telegram_account` was fintrack, every per-alert switch was on —
    and not one message had ever been delivered, because the deploy never exported
    FINTRACK_TELEGRAM_BOT_TOKEN/_CHAT_ID and compose substitutes `${...:-}` as an EMPTY STRING. The
    container held all four variables at length 0.

    That alone is a config bug. What made it invisible for weeks is this line: the drop was logged at
    DEBUG, so at the normal level a misconfigured deploy and a quiet market produce byte-identical
    logs. An operator reading the settings tab sees "Send Telegram alerts: on" and believes it.

    WARNING, not DEBUG. Asserting on the LEVEL is the point — a test that only checked the message
    text would pass against the broken version.
    """
    caplog.set_level(logging.WARNING, logger="api.notify.telegram")
    assert asyncio.run(TelegramNotifier(token="", chat_id="").send(Alert("x", "y"))) is False
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an unconfigured drop left no trace at WARNING — this is what hid the bug"
    assert "unconfigured" in warnings[0].getMessage().lower()


def test_the_unconfigured_WARNING_IS_SAID_ONCE_not_per_alert(caplog):
    """The original DEBUG was a deliberate choice: the docstring says treating a missing token as an
    error would mean "noisy logs everywhere". That intent is right and is preserved here — the warning
    fires once per notifier, not once per dropped alert. Without this, a per-poll alert loop would
    print a warning every few seconds and the signal would be re-buried under its own volume.
    """
    caplog.set_level(logging.WARNING, logger="api.notify.telegram")
    tx = TelegramNotifier(token="", chat_id="")
    for _ in range(5):
        asyncio.run(tx.send(Alert("x", "y")))
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning for five drops, got {len(warnings)}"


# -- durable dedupe ------------------------------------------------------------------------------
class FakeRedis:
    """SET NX EX semantics only — enough to prove check-and-mark is atomic and survives a restart."""

    def __init__(self, fail: bool = False):
        self.store, self.fail, self.ttls = {}, fail, {}

    async def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    async def delete(self, key):
        self.store.pop(key, None)


def _durable(tx, redis, settings=None):
    n = Notifier(transport=tx, settings=settings or ON)
    n._redis_client, n._redis_disabled = redis, False
    return n


def test_a_restart_does_not_re_announce_an_ongoing_condition():
    """THE bug: two identical HSBC drift alerts three minutes apart, one per API redeploy, because
    the dedupe lived only in process memory. A fresh Notifier is exactly what a restart produces."""
    redis, tx = FakeRedis(), FakeTx()
    asyncio.run(_durable(tx, redis).send("drift:HSBC", Alert("drift", "x"), now=_at(12)))
    assert len(tx.sent) == 1

    asyncio.run(_durable(tx, redis).send("drift:HSBC", Alert("drift", "x"), now=_at(12)))
    assert len(tx.sent) == 1, "a restart re-announced an already-announced condition"


def test_the_dedupe_key_carries_the_configured_ttl():
    """A permanent mark would mean a condition still true next week is never mentioned again."""
    redis, tx = FakeRedis(), FakeTx()
    n = _durable(tx, redis, {**ON, "repeat_after_hours": 6})
    asyncio.run(n.send("drift:X", Alert("d", "x"), now=_at(12)))
    assert list(redis.ttls.values()) == [6 * 3600]


def test_a_cleared_condition_drops_the_durable_key():
    redis, tx = FakeRedis(), FakeTx()
    n = _durable(tx, redis)
    asyncio.run(n.send("drift:X", Alert("d", "x"), now=_at(12)))
    assert redis.store

    async def go():
        n.clear("drift:X")
        await asyncio.sleep(0)              # let the best-effort drop task run
    asyncio.run(go())
    assert not redis.store


def test_redis_being_down_costs_a_duplicate_not_a_lost_alert():
    """The dedupe store must never be able to silence alerts — and Redis being down is itself an
    alert this system needs to deliver."""
    redis, tx = FakeRedis(fail=True), FakeTx()
    asyncio.run(_durable(tx, redis).send("subsystem_down:redis",
                                         Alert("down", "x", critical=True), now=_at(12)))
    assert len(tx.sent) == 1, "a failing dedupe store swallowed the alert"


# -- credential handling -------------------------------------------------------------------------
def test_the_bot_token_is_redacted_from_anything_logged():
    """The token is IN THE URL, and aiohttp embeds the URL in several of its exceptions
    (InvalidUrlClientError, ClientResponseError — both verified). Logging an exception verbatim would
    write a live credential into the container logs, where it goes wherever logs go."""
    tx = TelegramNotifier(token="SECRETTOKEN123", chat_id="chat")
    leaked = "ClientError('https://api.telegram.org/botSECRETTOKEN123/sendMessage')"
    assert "SECRETTOKEN123" not in tx._redact(leaked)
    assert "***" in tx._redact(leaked)


def test_redaction_does_not_hide_the_chat_id():
    """The chat id is not a credential, and knowing WHICH chat failed is the useful part."""
    tx = TelegramNotifier(token="tok", chat_id="-1001234567890")
    assert "-1001234567890" in tx._redact("failed for chat -1001234567890")


def test_redaction_is_safe_when_unconfigured():
    """An empty token must not turn into a str.replace('') that rewrites every character."""
    tx = TelegramNotifier(token="", chat_id="")
    assert tx._redact("nothing to hide") == "nothing to hide"


def test_a_delivery_error_is_logged_redacted(caplog, monkeypatch):
    """Drives the real `except` path, with no network.

    A first version of this test used a fake token against the live API and asserted on the logs. It
    passed — but Telegram answers a bad token with HTTP 401, which takes the STATUS path, never the
    exception path it claimed to cover. It also made a real network call from the unit suite. The
    failure has to be injected to be the failure under test.
    """
    import logging

    import aiohttp

    class Exploding:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, **k):
            raise aiohttp.InvalidUrlClientError(url)     # carries the URL, token and all

    monkeypatch.setattr(aiohttp, "ClientSession", Exploding)
    tx = TelegramNotifier(token="SECRETTOKEN123", chat_id="chat")
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(tx.send(Alert("t", "b"))) is False
    assert caplog.text, "the failure must still be logged — silent loss is worse than a redacted log"
    assert "SECRETTOKEN123" not in caplog.text
    assert "***" in caplog.text


# -- account selection ---------------------------------------------------------------------------
@pytest.mark.parametrize("account,expected", [
    ("fintrack", "FINTRACK_TELEGRAM_BOT_TOKEN"),
    ("kumo", "KUMO_TELEGRAM_BOT_TOKEN"),
    ("shared-hub", "SHAREDHUB_TELEGRAM_BOT_TOKEN"),   # hyphens stripped, per the shared script
])
def test_account_maps_to_the_shared_env_convention(account, expected):
    """Same mapping as ~/vibe-hq/tools/scripts/notify-telegram.sh, so an account provisioned for
    another project works here with no new bot and no new keychain entry."""
    assert env_keys(account)[0] == expected
    assert env_keys(account)[1] == expected.replace("BOT_TOKEN", "CHAT_ID")


def test_the_transport_reads_the_account_from_the_environment(monkeypatch):
    monkeypatch.setenv("FINTRACK_TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("FINTRACK_TELEGRAM_CHAT_ID", "chat")
    assert TelegramNotifier(account="fintrack").configured is True
    assert TelegramNotifier(account="kumo").configured is False, "must not fall back across accounts"


def test_the_account_setting_selects_the_transport(monkeypatch):
    """The account is a SETTING, so changing it in the UI must take effect without a restart — a
    transport captured in __init__ would keep sending as the old bot until redeploy."""
    monkeypatch.setenv("KUMO_TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("KUMO_TELEGRAM_CHAT_ID", "chat")
    n = Notifier(dedupe_backend="memory", settings={**ON, "telegram_account": "kumo"})
    assert n._transport(n._config()).account == "kumo"

    n2 = Notifier(dedupe_backend="memory", settings={**ON, "telegram_account": "fintrack"})
    assert n2._transport(n2._config()).account == "fintrack"


def test_a_missing_account_setting_defaults_to_fintrack():
    n = Notifier(dedupe_backend="memory", settings=ON)
    assert n._transport(n._config()).account == "fintrack"


def test_a_failed_send_does_not_hold_the_in_memory_dedupe_mark():
    """The redis mark is shortened to 15 minutes after a failed send; the in-memory fallback had no
    equivalent, so one transport blip silenced the key for the LIFE OF THE PROCESS — permanent loss
    wearing the dedupe's clothes (#651 item 3). A failed send must leave the key retryable."""
    import asyncio

    class Flaky:
        def __init__(self):
            self.sent = []
            self.fail_next = True

        async def send(self, alert, *, silent=False):
            if self.fail_next:
                self.fail_next = False
                return False
            self.sent.append(alert)
            return True

    tx = Flaky()
    n = Notifier(transport=tx, dedupe_backend="memory", settings={"enabled": True})
    assert asyncio.run(n.send("digest:open:2026-08-10", Alert("t", "b"))) is False
    assert tx.sent == [], "fixture property: the first attempt must actually fail"
    # Fifteen minutes pass. (The pacing itself — no burst inside the window — is pinned by
    # `test_a_failed_send_does_not_retry_every_poll` above; this instance ages instantly.)
    n._RETRY_AFTER_FAILURE_S = 0.0
    assert asyncio.run(n.send("digest:open:2026-08-10", Alert("t", "b"))) is True, (
        "retry after a failed send was deduped away — the in-memory mark outlived the failure"
    )
    # And a DELIVERED send still dedupes: the fix must not have loosened the success path.
    assert asyncio.run(n.send("digest:open:2026-08-10", Alert("t", "b"))) is False
    assert len(tx.sent) == 1


# -- formatting entities (#860) ------------------------------------------------------------------
#: The alert Telegram rejected twice on paper 2026-09-10 13:35Z, verbatim. Byte 289 of the formatted
#: text is the underscore in `reconcile_drift`, which legacy Markdown reads as an italic that never
#: closes. The retry went out plain and logged nothing, so whether the operator ever saw it is not
#: readable from the log.
_LIVE_TITLE = "stranded-claims check is NOT computing 1 symbol(s)"
_LIVE_BODY = ("Broker and cache disagree on TOST; a freeze there cannot be judged from the cache. "
              "Silence on these symbols is NOT 'nothing frozen'. Clears when reconcile_drift clears.")


def _bot_api_that_parses_markdown(posts: list, *, always_reject_markdown: bool = False):
    """A fake Bot API that does what Telegram did: with `parse_mode=Markdown`, an entity character
    (`_`, `*`, `` ` ``) that opens and never closes is HTTP 400 "can't parse entities". A backslash
    before the character is an escape, as in Telegram's legacy Markdown. Plain text always lands,
    with a message id, as the real API answers."""
    import json as _json
    import re as _re

    class _Resp:
        def __init__(self, status: int, body: str):
            self.status, self._body = status, body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return self._body

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, **k):
            posts.append(dict(json))
            if json.get("parse_mode") == "Markdown":
                text = json["text"]
                # an unclosed `[` is an unfinished link entity — rejected like the others
                unclosed_link = len(_re.findall(r"(?<!\\)\[", text)) > len(_re.findall(r"\]\(", text))
                for ch in ("_", "*", "`", "["):
                    odd = len(_re.findall(rf"(?<!\\){_re.escape(ch)}", text)) % 2 if ch != "[" else unclosed_link
                    if always_reject_markdown or odd:
                        off = text.encode().find(ch.encode())
                        return _Resp(400, _json.dumps({
                            "ok": False, "error_code": 400,
                            "description": f"Bad Request: can't parse entities: Can't find end of "
                                           f"the entity starting at byte offset {off}"}))
            return _Resp(200, '{"ok":true,"result":{"message_id":42}}')

    return _Session


def test_the_fake_rejects_the_live_text_when_it_is_NOT_escaped():
    """Fixture property first. If the fake accepted the raw live text, the test below would pass
    against a notifier that escapes nothing."""
    import asyncio as _asyncio

    from api.notify.telegram import _format

    raw = _format(Alert(_LIVE_TITLE, _LIVE_BODY), "kumo-cockpit", datetime(2026, 9, 10, 21, 35, tzinfo=SGT))
    assert "reconcile\\_drift" in raw, "the live body's identifier must be escaped by _format itself"
    posts: list = []
    session = _bot_api_that_parses_markdown(posts)()
    resp = _asyncio.run(session.post("u", json={"text": "Clears when reconcile_drift clears.",
                                                "parse_mode": "Markdown"}).__aenter__())
    assert resp.status == 400, "the fake does not reject an unpaired underscore — it cannot see #860"


def test_the_live_stranded_claims_alert_is_accepted_FORMATTED_on_the_first_post(monkeypatch):
    """The defect: the first post was rejected on the underscore and the alert only landed on the
    plain retry. The text is escaped for the parse mode in use, so the FIRST post lands, formatted,
    and no retry happens. Red before #860: two posts, the first a 400."""
    import aiohttp

    posts: list = []
    monkeypatch.setattr(aiohttp, "ClientSession", _bot_api_that_parses_markdown(posts))
    ok = asyncio.run(TelegramNotifier(token="t", chat_id="c").send(Alert(_LIVE_TITLE, _LIVE_BODY)))
    assert ok is True
    assert len(posts) == 1, f"the formatted message was rejected and retried plain: {posts}"
    assert posts[0]["parse_mode"] == "Markdown"
    assert "reconcile\\_drift" in posts[0]["text"], "the underscore reached Telegram unescaped"
    assert posts[0]["text"].startswith("*kumo-cockpit*"), "the prefix must stay bold — escaping is for the alert's own text"


def test_the_plain_retry_OUTCOME_is_logged_not_silent(caplog, monkeypatch):
    """When formatting is still rejected for a reason escaping cannot fix, the plain retry must
    leave a record of what happened — delivered (with Telegram's message id) or not. On paper the
    retry logged nothing, so 'sent' and 'lost' looked the same."""
    import aiohttp

    posts: list = []
    monkeypatch.setattr(aiohttp, "ClientSession",
                        _bot_api_that_parses_markdown(posts, always_reject_markdown=True))
    with caplog.at_level(logging.INFO, logger="api.notify.telegram"):
        ok = asyncio.run(TelegramNotifier(token="t", chat_id="c").send(Alert(_LIVE_TITLE, _LIVE_BODY)))
    assert ok is True and len(posts) == 2 and "parse_mode" not in posts[1]
    delivered = [r for r in caplog.records if "plain" in r.getMessage().lower() and "deliver" in r.getMessage().lower()]
    assert delivered, f"the plain retry landed and nothing said so: {[r.getMessage() for r in caplog.records]}"
    assert "42" in delivered[0].getMessage(), "the record must carry Telegram's message id — proof it landed"
    assert _LIVE_TITLE in delivered[0].getMessage()


def test_markup_the_alert_AUTHORS_wrote_still_reaches_the_reader(monkeypatch):
    """Nine alert sites bold a symbol (`*TOST*`, alerts.py:100/103/106, pool_sweep.py:55/63) or wrap
    detail in backticks (alerts.py:83/125/638/851) on purpose. Escaping every entity character made
    those render as literal asterisks and backticks (review-860). Only the characters no author ever
    means as markup — `_` in an identifier, `[` in venue text — are escaped."""
    import aiohttp

    posts: list = []
    monkeypatch.setattr(aiohttp, "ClientSession", _bot_api_that_parses_markdown(posts))
    ok = asyncio.run(TelegramNotifier(token="t", chat_id="c").send(
        Alert("Broker sync drift · TOST", "*TOST* — broker 100, cockpit 0. Detail: `reconcile_drift`")))
    assert ok is True and len(posts) == 1, posts
    assert "*TOST*" in posts[0]["text"], "the author's bold was escaped away"
    assert "`reconcile\\_drift`" in posts[0]["text"], "the underscore inside a code span still needs escaping under legacy Markdown"


def test_an_unclosed_bracket_from_venue_text_is_escaped_too(monkeypatch):
    """`[` opens a link entity in legacy Markdown and arrives through `{detail}`/`{reason}` from the
    venue (alerts.py:83, :125). Without this case the `[` in the escape set was earning nothing:
    dropping it left the suite green (review-860)."""
    import aiohttp

    posts: list = []
    monkeypatch.setattr(aiohttp, "ClientSession", _bot_api_that_parses_markdown(posts))
    ok = asyncio.run(TelegramNotifier(token="t", chat_id="c").send(
        Alert("engine down", "venue said: order not found [id 6b87d544")))
    assert ok is True and len(posts) == 1, posts
    assert "\\[id" in posts[0]["text"]


def test_a_body_read_failure_AFTER_a_200_does_not_turn_delivery_into_a_loss(monkeypatch, caplog):
    """The retry's outcome record reads the response body. If that read raises after Telegram
    already answered 200, the alert WAS delivered: reporting False re-sends it on the next poll and
    the digest path reads it as permanently lost (review-860, BLOCKING)."""
    import aiohttp

    posts: list = []
    base = _bot_api_that_parses_markdown(posts, always_reject_markdown=True)

    class _Session(base):
        def post(self, url, json=None, **k):
            resp = super().post(url, json=json, **k)
            if "parse_mode" not in json:
                async def _boom():
                    raise aiohttp.ClientPayloadError("body cut")
                resp.text = _boom
            return resp

    monkeypatch.setattr(aiohttp, "ClientSession", _Session)
    with caplog.at_level(logging.INFO, logger="api.notify.telegram"):
        ok = asyncio.run(TelegramNotifier(token="t", chat_id="c").send(Alert(_LIVE_TITLE, _LIVE_BODY)))
    assert ok is True, "Telegram answered 200 on the retry; the alert was delivered"
    delivered = [r for r in caplog.records if "delivered plain" in r.getMessage().lower()]
    assert delivered and "unreadable" in delivered[0].getMessage()
